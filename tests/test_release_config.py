"""Repository-level release wiring: every package registered, everywhere, consistently.

These are not any one package's tests — they are about the five files that have to agree for
a release to happen at all (`release-please-config.json`, `.release-please-manifest.json`,
`uv.lock`, `publish-packages.yml` and `pr-title.yml`), which is why they live at the root
rather than under a package.

Each failure here is one that is otherwise silent: a new package that release-please never
proposes a release for, a manifest that has drifted from the version actually declared, a
component that tags as something the publish workflow does not listen for, or two packages
whose tags collide. None of those break a test, a type check or a build — they break a
release, at the one moment when the thing that went wrong is hardest to undo.
"""

from __future__ import annotations

import fnmatch
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "release-please-config.json"
MANIFEST_PATH = REPO_ROOT / ".release-please-manifest.json"
LOCK_PATH = REPO_ROOT / "uv.lock"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLISH_WORKFLOW = WORKFLOWS / "publish-packages.yml"
PR_TITLE_WORKFLOW = WORKFLOWS / "pr-title.yml"
RELEASE_WORKFLOW = WORKFLOWS / "release-please.yml"

CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
LOCK = tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))

# Every directory under packages/ that is actually a distribution.
PACKAGE_PATHS = sorted(
    str(path.parent.relative_to(REPO_ROOT)).replace("\\", "/")
    for path in REPO_ROOT.glob("packages/*/pyproject.toml")
)


def pyproject(package_path: str) -> dict:
    return tomllib.loads(
        (REPO_ROOT / package_path / "pyproject.toml").read_text("utf-8")
    )


def declared_version(package_path: str) -> str:
    return pyproject(package_path)["project"]["version"]


def declared_name(package_path: str) -> str:
    return pyproject(package_path)["project"]["name"]


def configured_component(package_path: str) -> str:
    """What release-please will actually put in the tag.

    `package-name`, not the distribution name in `pyproject.toml`: the Python strategy reads
    that file only to find version-bearing sources, never to name the component. An entry
    without it leaves the component empty, and every package tags as a bare `v<version>`.
    """
    return CONFIG["packages"][package_path]["package-name"]


def release_tag(package_path: str) -> str:
    return f"{configured_component(package_path)}-v{declared_version(package_path)}"


def lock_jsonpath(distribution: str) -> str:
    """Points an `extra-files` updater at one `[[package]]` entry in `uv.lock`.

    `.value` rather than `name` itself: release-please parses TOML into position-annotated
    nodes (`{start, end, value}`), so a filter comparing `@.name` to a string compares an
    object and matches nothing. That shape is its internal parser's, not a documented
    contract — if a release-please bump breaks it, the symptom is a Release PR whose
    `uv sync --locked` check fails, and the fix is to re-check this expression.
    """
    return f"$.package[?(@.name.value=='{distribution}')].version"


def locked_version(distribution: str) -> str | None:
    for entry in LOCK["package"]:
        if entry.get("name") == distribution:
            return entry.get("version")
    return None


def lock_updater(package_path: str) -> dict:
    entries = CONFIG["packages"][package_path].get("extra-files", [])
    assert len(entries) == 1, (
        f"{package_path}: expected one extra-files entry, got {entries}"
    )
    return entries[0]


def publish_tag_globs() -> list[str]:
    """The `on.push.tags` globs, read out of the workflow without a YAML dependency."""
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"^ *tags:\n((?: *- *\"[^\"]+\"\n)+)", workflow, re.MULTILINE)
    assert block is not None, (
        f"no `on.push.tags` block found in {PUBLISH_WORKFLOW.name}"
    )
    return re.findall(r"\"([^\"]+)\"", block.group(1))


def accepted_title_types() -> list[str]:
    """The commit types the PR title check allows, from its `types: |` block scalar."""
    workflow = PR_TITLE_WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"^ *types: \|\n((?: +[a-z]+\n)+)", workflow, re.MULTILINE)
    assert block is not None, f"no `types:` block found in {PR_TITLE_WORKFLOW.name}"
    return block.group(1).split()


class TestEveryPackageIsRegistered:
    """A package missing from either file is one that never gets released, quietly."""

    def test_packages_dir_and_release_please_config_agree(self):
        assert sorted(CONFIG["packages"]) == PACKAGE_PATHS

    def test_packages_dir_and_manifest_agree(self):
        assert sorted(MANIFEST) == PACKAGE_PATHS


class TestManifestMatchesDeclaredVersions:
    """release-please bumps from the manifest; the workflow validates against pyproject.

    They are two records of one fact, so a drift between them is only discovered at release
    time — as either a wrong proposed bump or a tag that the publish gate rejects.
    """

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_manifest_version_matches_pyproject(self, package_path: str):
        assert MANIFEST[package_path] == declared_version(package_path)


class TestTheLockRecordsTheVersionEachPackageDeclares:
    """`uv.lock` is a fifth file that has to agree, and the last one nothing updated.

    A release bumps `pyproject.toml` and the lock keeps naming the previous version. Nothing
    surfaces that on its own — a plain `uv sync` re-locks in the runner rather than failing —
    so CI stays green while a contributor's first sync leaves an uncommitted change in a
    generated file they never touched.
    """

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_locked_version_matches_pyproject(self, package_path: str):
        distribution = declared_name(package_path)
        assert locked_version(distribution) == declared_version(package_path), (
            f"uv.lock is stale for {distribution} — run `uv lock`"
        )


class TestEveryPackageUpdatesTheLockWhenItReleases:
    """What keeps the agreement above true at the one moment it breaks.

    release-please knows nothing about `uv.lock`, so each package points an `extra-files`
    updater at its own entry. A package without one releases perfectly happily and leaves the
    lock a version behind, which is exactly how this was found.
    """

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_the_updater_targets_the_lockfile(self, package_path: str):
        entry = lock_updater(package_path)
        assert entry["type"] == "toml"
        # The leading slash is the whole mechanism: release-please resolves an extra-file
        # path against the package directory, and rejects `../` outright rather than
        # walking up. Without it this silently addresses a lockfile inside the package.
        assert entry["path"].startswith("/")
        assert REPO_ROOT / entry["path"].lstrip("/") == LOCK_PATH

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_the_updater_selects_this_package_entry(self, package_path: str):
        assert lock_updater(package_path)["jsonpath"] == lock_jsonpath(
            declared_name(package_path)
        )


class TestComponentMatchesDistributionName:
    """The tag's component is configuration, and nothing derives it from the package.

    So it can drift from the name it is supposed to mirror — silently, because release-please
    would go on tagging happily under the wrong component while the publish workflow, which
    maps a tag back to a directory, listens for the right one and never fires.
    """

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_configured_component_is_the_distribution_name(self, package_path: str):
        assert configured_component(package_path) == declared_name(package_path)

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_directory_basename_is_the_distribution_name(self, package_path: str):
        """release-please.yml dispatches the publish with `package=` the path's basename.

        The publish workflow takes that as both the directory to build and the distribution
        to upload, so a package whose directory and `[project] name` disagree would dispatch
        a release of the wrong thing — or of nothing, since the input is a fixed choice list.
        """
        assert package_path.rsplit("/", 1)[-1] == declared_name(package_path)


class TestTagsResolveToExactlyOnePackage:
    """`maf-sandbox-v*` must not also swallow `maf-sandbox-acas-v0.1.0`.

    It does not, because the character after `maf-sandbox-` there is `a` rather than `v` —
    but that is a property of these particular names, not of the scheme, so a fourth package
    could quietly break it. Two globs matching one tag means two publish runs for one release.
    """

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_each_package_tag_matches_exactly_one_glob(self, package_path: str):
        tag = release_tag(package_path)
        matched = [
            glob for glob in publish_tag_globs() if fnmatch.fnmatchcase(tag, glob)
        ]
        assert matched == [f"{configured_component(package_path)}-v*"], (
            f"tag {tag} matched {matched}"
        )

    def test_no_glob_is_orphaned(self):
        tags = [release_tag(path) for path in PACKAGE_PATHS]
        for glob in publish_tag_globs():
            assert any(fnmatch.fnmatchcase(tag, glob) for tag in tags), (
                f"glob {glob} matches no package — a rename left it behind"
            )


class TestAcceptedTitleTypesAreConfigured:
    """A title type the check allows but the changelog config never mentions is a hole.

    release-please treats an unconfigured type as hidden, so such a commit lands on `main`
    with a green check and then releases nothing and appears nowhere — the one outcome
    neither file claims. Keeping the two lists equal is what makes the documented table true.
    """

    def test_the_two_lists_are_the_same_set(self):
        configured = {section["type"] for section in CONFIG["changelog-sections"]}
        assert sorted(accepted_title_types()) == sorted(configured)


class TestOnlyUserFacingTypesRelease:
    """Which types cut a release is a decision, and it lives in `hidden`.

    Every visible type releases: release-please bumps on any commit that produces a changelog
    entry, patch unless it is a `feat` or breaking. So `docs` visible is deliberate — a
    package's README is its PyPI front page, and publishing is the only way to change it —
    and `refactor` hidden is too, since a refactor is by definition not user-facing.
    """

    def test_the_releasing_types_are_exactly_these(self):
        visible = {
            section["type"]
            for section in CONFIG["changelog-sections"]
            if not section.get("hidden", False)
        }
        assert visible == {"feat", "fix", "perf", "revert", "docs"}


class TestReleasesAreNotDrafted:
    """`draft` looks like the way to keep a Release behind the upload it announces. It isn't.

    A draft carries no tag, and a tag is how release-please finds where the last release
    ended: the release iterator skips releases with no tag commit, the tag backfill has
    nothing to find, and the manifest fallback synthesises a release with `sha: ''`. An empty
    sha matches no commit, so `commitsAfterSha` returns the whole history — and because the
    action creates releases and then pull requests in one invocation, every release would
    immediately open a second Release PR replaying what had just shipped.

    So the Release exists before the upload does. That is a knowing trade, written up in
    docs/maintainers.md, and re-adding `draft` to undo it breaks releases instead.
    """

    def test_draft_is_off(self):
        assert CONFIG.get("draft", False) is False

    def test_component_is_in_the_tag(self):
        # Without this, all three packages would tag as plain `v<version>` and collide.
        assert CONFIG["include-component-in-tag"] is True


class TestReleasePullRequestsKeepThemselvesCurrent:
    """Off, merging one package's release strands the others.

    release-please updates a release branch only when the notes it would write change, so
    the packages that did not change stay pinned to an old `main` — and every release moves
    the manifest and the lockfile, which is what they then conflict on. The recovery was to
    delete those branches and rebuild them, which loses the approvals on their checks.
    """

    def test_always_update_is_on(self):
        assert CONFIG["always-update"] is True


class TestDependentsPinMafSandboxInAShapeTheRangeScriptCanRead:
    """`scripts/set_dependents_range.py` moves both ends of the range after a release, by pattern.

    Editing by pattern silently no-ops when the string drifts, so a dependent that reformats
    its maf-sandbox constraint would quietly stop being bumped while the release step still
    reported success. This pins every dependent's constraint to the `maf-sandbox>=X,<Y` shape
    the script parses, so the workflow and the files cannot drift apart unnoticed.
    """

    _MAF_SANDBOX = re.compile(r"maf-sandbox>=\d+(?:\.\d+)*,<\d+(?:\.\d+)*")
    # The base package exactly — the lookahead keeps `maf-sandbox-acas` from being read as it.
    _IS_BASE = re.compile(r"maf-sandbox(?![-\w])")

    def _base_constraint(self, package_path: str) -> str | None:
        if declared_name(package_path) == "maf-sandbox":
            return None
        deps = pyproject(package_path)["project"].get("dependencies", [])
        return next((d for d in deps if self._IS_BASE.match(d)), None)

    def test_every_dependent_is_pinned_in_the_readable_shape(self):
        constraints = {
            package_path: self._base_constraint(package_path)
            for package_path in PACKAGE_PATHS
        }
        dependents = {p: c for p, c in constraints.items() if c is not None}
        assert dependents, "expected at least one package to depend on maf-sandbox"
        for package_path, constraint in dependents.items():
            assert self._MAF_SANDBOX.fullmatch(constraint), (
                f"{package_path}: {constraint!r} is not the maf-sandbox>=X,<Y shape "
                "scripts/set_dependents_range.py edits by pattern"
            )


class TestRoutineAutomationDoesNotClaimToCloseAnIssue:
    """A pull request the release workflow opens every cycle cannot close a specific issue.

    `release-please.yml` writes the floor-bump pull request's body from a template. It once
    carried `Closes #41.` — true of the one pull request that built the automation, and
    inherited by every bump it has emitted since, each telling a reader it closes an issue it
    has nothing to do with. A closing keyword belongs in a pull request written once, never in
    a body a workflow re-emits.
    """

    _CLOSING_KEYWORD = re.compile(
        r"\b(?:closes|closed|fixes|fixed|resolves|resolved)\s+#\d+", re.I
    )

    def test_the_release_workflow_writes_no_closing_keyword(self):
        text = RELEASE_WORKFLOW.read_text(encoding="utf-8")
        found = self._CLOSING_KEYWORD.findall(text)
        assert not found, (
            f"release-please.yml emits {found} into a pull request body it writes every "
            "release; the issue it names gets re-referenced forever. Drop the keyword."
        )


class TestTheProposalBodySurvivesTheShell:
    """The pull request body the release workflow writes must still be prose after bash reads it.

    It was not, once. The body was a multi-line double-quoted assignment, and the quotes around
    `"Approve and run"` in the last paragraph closed the string: bash read the remainder as
    commands and the step exited 127 on `and: command not found`. It failed after the tag and
    after the publish dispatch, so the release was whole and only the proposal was lost — a
    branch pushed to the remote with no pull request on it, which nothing reports.

    `bash -n` does not catch this; the broken form parses cleanly and only misbehaves when run.
    So this executes the fragment for real, against a temporary directory, and reads the file
    it writes. A test that only checked syntax would have passed on the exact bug it was for.
    """

    _EXPECTED = (
        # The phrase that broke it. Quotes are the failure mode, so this is the assertion.
        'held at "Approve and run", the same as a Release PR\'s',
        "**Check that it actually published before merging this.**",
        "**The ceiling is uniform and is a claim about nobody's code.**",
        "**The floor is a guess, and usually a wrong one.**",
        "**To decline a floor without losing its ceiling**",
        "**Then merge it, and let the dependent releases it cuts publish.**",
    )

    def _run_block(self, step_name: str) -> str:
        """The step's `run:` script, dedented — the same text the runner's shell receives.

        Read as text rather than through a YAML parser, for the reason the module docstring
        gives: these tests carry no YAML dependency, and a block scalar is unambiguous enough
        to slice on indentation.
        """
        lines = RELEASE_WORKFLOW.read_text(encoding="utf-8").splitlines()
        start = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == f"- name: {step_name}"
        )
        run = next(
            index
            for index, line in enumerate(lines[start:], start)
            if line.strip() == "run: |"
        )
        indent = len(lines[run]) - len(lines[run].lstrip()) + 2
        body: list[str] = []
        for line in lines[run + 1 :]:
            if line.strip() and not line.startswith(" " * indent):
                break
            body.append(line[indent:])
        return "\n".join(body)

    def _body_fragment(self) -> str:
        """Just the heredoc through the substitution — no git, gh or python3 to stand up."""
        lines = self._run_block("Propose the dependents' range").splitlines()
        start = next(
            (i for i, line in enumerate(lines) if line.startswith("cat > ")), None
        )
        end = next(
            (i for i, line in enumerate(lines) if line.startswith("sed -i ")), None
        )
        assert start is not None and end is not None, (
            "the step no longer builds the body in a file — if it has gone back to a shell "
            "variable, the prose is being parsed by bash again, which is the bug this is for"
        )
        assert start < end, "the substitution must follow the heredoc that needs it"
        return "\n".join(lines[start : end + 1])

    def test_the_body_is_written_intact(self, tmp_path: Path):
        if shutil.which("bash") is None:
            pytest.skip("no bash on PATH; the release runner is ubuntu-latest")
        script = (
            "set -euo pipefail\n"
            "VERSION=1.2.3\n"
            # `.` with the process started in tmp_path, rather than an absolute path: a
            # Windows checkout may resolve `bash` to one that cannot read `C:/...`, and the
            # test would then fail on the path instead of testing the body.
            "RUNNER_TEMP=.\n"
            f"{self._body_fragment()}\n"
            'cat "$RUNNER_TEMP/range-body.md"\n'
        )
        # Through stdin rather than a path: a Windows checkout would otherwise hand a
        # drive-lettered path to a shell that does not read one, and skip for the wrong reason.
        # Bytes rather than `text=True`, because that translates the newlines on the way in and
        # a shell handed `set -euo pipefail\r` rejects the option instead of the prose.
        result = subprocess.run(
            ["bash", "-s"],
            input=script.encode("utf-8"),
            capture_output=True,
            cwd=tmp_path,
        )
        stdout = result.stdout.decode("utf-8")
        assert result.returncode == 0, (
            f"the body fragment did not survive bash (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace').strip()}"
        )
        for phrase in self._EXPECTED:
            assert phrase in stdout, f"the body lost {phrase!r}"
        assert "1.2.3" in stdout, "the version was never substituted"
        assert "@VERSION@" not in stdout, "a placeholder reached the pull request body"

    def test_the_heredoc_does_not_expand(self):
        """Quoting the delimiter is what makes the prose inert, and it has to stay quoted.

        The test above would pass on an unquoted heredoc too, because nothing in today's body
        expands. The next paragraph is the risk: this repository writes `maf-sandbox` and
        `${VERSION}` in backticks everywhere else, and a backtick in an unquoted heredoc is
        command substitution — the same failure again, with a different character.
        """
        fragment = self._body_fragment()
        assert re.search(r"<<'\w+'", fragment), (
            "the body heredoc must quote its delimiter (`<<'BODY'`) so nothing in the prose "
            "is expanded; the version is substituted afterwards instead"
        )


class TestTheConstraintCommentsDoNotNameAVersion:
    """The prose above a maf-sandbox constraint must not name the release it points at.

    `scripts/set_dependents_range.py` rewrites the constraint and never the comment above
    it, so a sentence naming a version is stale one release later and no test noticed. Every
    such sentence in this repository had drifted by at least two minors before anyone read
    one. The constraint below is the source of truth; the comment says what the floor is
    *for*. A bare `1.0.0` is fine — that is the stability boundary, not a release pointer.
    """

    _ZERO_VERSION = re.compile(r"0\.\d+\.\d+")

    def _comment_block_above_the_constraint(self, package_path: str) -> list[str]:
        lines = (
            (REPO_ROOT / package_path / "pyproject.toml")
            .read_text("utf-8")
            .splitlines()
        )
        for index, line in enumerate(lines):
            if "maf-sandbox>=" not in line:
                continue
            above: list[str] = []
            cursor = index - 1
            while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
                above.append(lines[cursor])
                cursor -= 1
            return above
        return []

    def test_no_dependent_names_a_maf_sandbox_release_in_prose(self):
        checked = 0
        for package_path in PACKAGE_PATHS:
            block = self._comment_block_above_the_constraint(package_path)
            if not block:
                continue
            checked += 1
            for line in block:
                assert not self._ZERO_VERSION.search(line), (
                    f"{package_path}: {line.strip()!r} names a maf-sandbox release. The bump "
                    "script moves the constraint and not this comment, so the number goes "
                    "stale — say which release the floor is for, not which release that is."
                )
        assert checked, (
            "expected at least one package to carry a maf-sandbox constraint"
        )
