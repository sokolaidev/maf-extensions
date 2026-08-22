"""Repository-level release wiring: every package registered, everywhere, consistently.

These are not any one package's tests — they are about the files that have to agree for a
release to happen at all (`release-please-config.json`, `.release-please-manifest.json`,
`uv.lock`, `publish-packages.yml` and `pr-title.yml`), and about `RELEASING.md`, which tells a
maintainer what those files are going to do. Which is why they live at the root rather than
under a package.

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
import urllib.error
import urllib.parse
from pathlib import Path

import pytest
from _version_prose import release_named_in

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "release-please-config.json"
MANIFEST_PATH = REPO_ROOT / ".release-please-manifest.json"
LOCK_PATH = REPO_ROOT / "uv.lock"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLISH_WORKFLOW = WORKFLOWS / "publish-packages.yml"
PR_TITLE_WORKFLOW = WORKFLOWS / "pr-title.yml"
RELEASE_WORKFLOW = WORKFLOWS / "release-please.yml"
RELEASING = REPO_ROOT / "RELEASING.md"

CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
LOCK = tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))

# Every directory under packages/ that is actually a distribution.
PACKAGE_PATHS = sorted(
    str(path.parent.relative_to(REPO_ROOT)).replace("\\", "/")
    for path in REPO_ROOT.glob("packages/*/pyproject.toml")
)


def pyproject(package_path: str) -> dict:
    return tomllib.loads((REPO_ROOT / package_path / "pyproject.toml").read_text("utf-8"))


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
    assert len(entries) == 1, f"{package_path}: expected one extra-files entry, got {entries}"
    return entries[0]


def publish_tag_globs() -> list[str]:
    """The `on.push.tags` globs, read out of the workflow without a YAML dependency."""
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"^ *tags:\n((?: *- *\"[^\"]+\"\n)+)", workflow, re.MULTILINE)
    assert block is not None, f"no `on.push.tags` block found in {PUBLISH_WORKFLOW.name}"
    return re.findall(r"\"([^\"]+)\"", block.group(1))


def run_block(workflow: Path, step_name: str) -> str:
    """A step's `run:` script, dedented — the same text the runner's shell receives.

    Read as text rather than through a YAML parser, for the reason the module docstring gives:
    these tests carry no YAML dependency, and a block scalar is unambiguous enough to slice on
    indentation.
    """
    lines = workflow.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.strip() == f"- name: {step_name}"
    )
    run = next(index for index, line in enumerate(lines[start:], start) if line.strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body: list[str] = []
    for line in lines[run + 1 :]:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line[indent:])
    return "\n".join(body)


def condition_after(workflow: Path, anchor: str) -> str:
    """`anchor`'s own `if: >-` folded block, as one whitespace-normalised line.

    `anchor` is a stripped line — a job's key or a step's `- name: …`. The search stops where
    that mapping does, at the next line indented no further, and an anchor carrying no `if:`
    fails rather than borrowing one: unbounded, a job whose gate was deleted returns the *next*
    job's, and a test asserting on the gate goes on passing with no gate there.
    """
    lines = workflow.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == anchor)
    depth = len(lines[start]) - len(lines[start].lstrip())
    mapping: list[str] = []
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) <= depth:
            break
        mapping.append(line)
    marker = next((index for index, line in enumerate(mapping) if line.strip() == "if: >-"), None)
    assert marker is not None, (
        f"{anchor} carries no `if: >-` block in {workflow.name}; if its gate was removed, "
        "that is the change to look at rather than this helper"
    )
    indent = len(mapping[marker]) - len(mapping[marker].lstrip()) + 2
    body: list[str] = []
    for line in mapping[marker + 1 :]:
        if not line.startswith(" " * indent):
            break
        body.append(line.strip())
    return " ".join(body)


def _job_block(workflow: Path, job_key: str) -> str:
    """One job's body as text, from its key line to the next job's key line.

    Preceding comment lines stay with the previous job's block, so the block starts at the key.
    Read as text, no YAML dependency, for the same reason `run_block` gives. The next-job scan
    matches only a bare key at the jobs-map indent (two spaces), so a `run:` script line — however
    deeply indented — cannot end the block early.
    """
    lines = workflow.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip() == job_key)
    end = next(
        (
            index
            for index, line in enumerate(lines[start + 1 :], start + 1)
            if re.match(r"^  [A-Za-z][\w-]*:\s*$", line)
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def dispatched_packages() -> set[str]:
    """The packages whose publish dispatches the live check, from the `verify` gate itself."""
    condition = condition_after(PUBLISH_WORKFLOW, "verify:")
    listed = re.search(r"fromJSON\('(\[[^\]]*\])'\)", condition)
    assert listed is not None, (
        f"the `verify` gate in {PUBLISH_WORKFLOW.name} no longer names its packages in a "
        "fromJSON list"
    )
    return set(json.loads(listed.group(1)))


def packages_named_in_releasing() -> set[str]:
    """Every maf-sandbox distribution RELEASING.md's live-check paragraph names."""
    marker = "a live check runs on its own"
    line = next(
        (line for line in RELEASING.read_text("utf-8").splitlines() if marker in line),
        None,
    )
    assert line is not None, f"no paragraph in RELEASING.md says {marker!r}"
    return set(re.findall(r"`(maf-sandbox[\w-]*)`", line))


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
        assert lock_updater(package_path)["jsonpath"] == lock_jsonpath(declared_name(package_path))


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
        matched = [glob for glob in publish_tag_globs() if fnmatch.fnmatchcase(tag, glob)]
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
            package_path: self._base_constraint(package_path) for package_path in PACKAGE_PATHS
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

    _CLOSING_KEYWORD = re.compile(r"\b(?:closes|closed|fixes|fixed|resolves|resolved)\s+#\d+", re.I)

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

    def _body_fragment(self) -> str:
        """Just the heredoc through the substitution — no git, gh or python3 to stand up."""
        lines = run_block(RELEASE_WORKFLOW, "Propose the dependents' range").splitlines()
        start = next((i for i, line in enumerate(lines) if line.startswith("cat > ")), None)
        end = next((i for i, line in enumerate(lines) if line.startswith("sed -i ")), None)
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

    def _comment_lines_beside_the_constraint(self, package_path: str) -> list[str]:
        """The comment block above the constraint, **and the constraint's own line**.

        Its own line, because that is where a trailing comment goes and the bump script
        rewrites exactly that line: `"maf-sandbox>=0.16.0,<0.18",  # 0.13 for the work_dir
        default` is legal TOML, is the most natural place to write the note, and walking only
        upwards never looked at it (#385).
        """
        lines = (REPO_ROOT / package_path / "pyproject.toml").read_text("utf-8").splitlines()
        for index, line in enumerate(lines):
            if "maf-sandbox>=" not in line:
                continue
            beside = [line]
            cursor = index - 1
            while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
                beside.append(lines[cursor])
                cursor -= 1
            return beside
        return []

    def test_no_dependent_names_a_maf_sandbox_release_in_prose(self):
        checked = 0
        for package_path in PACKAGE_PATHS:
            beside = self._comment_lines_beside_the_constraint(package_path)
            if not beside:
                continue
            checked += 1
            for line in beside:
                named = release_named_in(line)
                assert named is None, (
                    f"{package_path}: {line.strip()!r} names {named}. The bump script moves "
                    "the constraint and not this comment, so the number goes stale — say "
                    "which release the floor is for, not which release that is."
                )
        assert checked, "expected at least one package to carry a maf-sandbox constraint"

    def test_the_stability_boundary_every_package_names_is_still_allowed(self):
        """The exemption, asserted where it would break rather than only in the reader's tests.

        All five dependency comments say some form of "every release before 1.0.0 may include
        breaking changes". A pattern matching the `0.0` inside `1.0.0` fails all five for
        saying the one thing this guard means to permit — which is what the first attempt at
        widening it did.
        """
        mentions = [
            package_path
            for package_path in PACKAGE_PATHS
            if any(
                "1.0.0" in line for line in self._comment_lines_beside_the_constraint(package_path)
            )
        ]
        assert mentions, (
            "no package comment mentions 1.0.0 any more, so this test guards nothing — either "
            "the boilerplate changed or the reader stopped finding these lines"
        )


class TestReleasingNamesEveryPackageThatDispatchesTheLiveCheck:
    """The instructions and the gate are two records of one list, and only one of them runs.

    RELEASING.md named three of the five for two releases after `maf-sandbox-codeact` and
    `maf-sandbox-docker` joined the gate. Nothing surfaced it: a maintainer reading the wrong
    list is not a failing check, it is someone not expecting a workflow that then runs.
    """

    def test_the_paragraph_and_the_gate_name_the_same_packages(self):
        assert packages_named_in_releasing() == dispatched_packages()


_TWO_JOBS_ONE_GATED = """jobs:
  ungated:
    needs: [build]
    runs-on: ubuntu-latest

  gated:
    if: >-
      needs.build.outputs.target == 'pypi'
      && needs.build.outputs.breaking != 'true'
    runs-on: ubuntu-latest
"""


class TestReadingAGateOutOfTheWorkflow:
    """`condition_after` is what every gate assertion below rests on, so it has to be exact.

    The failure that matters is the quiet one: a search running past its own job returns the
    next job's `if:`, which reads as a gate that is still there. Every test asserting on a gate
    then passes on a workflow that has none.
    """

    def _workflow(self, tmp_path: Path) -> Path:
        path = tmp_path / "w.yml"
        path.write_text(_TWO_JOBS_ONE_GATED, encoding="utf-8")
        return path

    def test_a_job_with_no_gate_does_not_borrow_the_next_one(self, tmp_path: Path):
        with pytest.raises(AssertionError, match="carries no `if: >-` block"):
            condition_after(self._workflow(tmp_path), "ungated:")

    def test_a_gated_job_reads_its_own(self, tmp_path: Path):
        condition = condition_after(self._workflow(tmp_path), "gated:")
        assert condition == (
            "needs.build.outputs.target == 'pypi' && needs.build.outputs.breaking != 'true'"
        )


def _execute_step(tmp_path: Path, step: str, stub: str) -> subprocess.CompletedProcess[bytes]:
    """Run a publish step's `run:` block with `python3` shadowed, the way the runner would.

    The step is executed for real, `python3` shadowed by a shell function, because the failure
    that matters is one bash makes rather than one the text shows: `set -e` is on in these
    steps, so a verdict read as a plain `output="$(…)"` assignment ends the whole step when the
    script exits non-zero — refusing a release over a decision about a later job, which is the
    behaviour that has to hold or break in the right direction.
    """
    if shutil.which("bash") is None:
        pytest.skip("no bash on PATH; the release runner is ubuntu-latest")
    script = (
        "PACKAGE=maf-sandbox\n"
        "VERSION=0.13.0\n"
        "GITHUB_OUTPUT=out.txt\n"
        "GITHUB_STEP_SUMMARY=summary.md\n"
        ": > out.txt\n"
        ": > summary.md\n"
        # A function, not a file on PATH: no exec bit to set, and it shadows the command
        # the step calls on any runner.
        f"{stub}\n"
        f"{run_block(PUBLISH_WORKFLOW, step)}\n"
    )
    # Bytes rather than `text=True`: that translates the newlines on the way in, and a shell
    # handed `set -euo pipefail\r` rejects the option instead of running the script.
    return subprocess.run(
        ["bash", "-s"],
        input=script.encode("utf-8"),
        capture_output=True,
        cwd=tmp_path,
    )


def _step_outputs(tmp_path: Path) -> tuple[str, str]:
    return (
        (tmp_path / "out.txt").read_text("utf-8"),
        (tmp_path / "summary.md").read_text("utf-8"),
    )


class TestTheBreakingDetectorIsInformational:
    """The changelog flag is reported as context but does not control live-check dispatch.

    The work check owns the dispatch decision; this detector must remain non-gating.
    """

    _STEP = "Check whether this release is breaking"

    def test_a_breaking_release_is_reported_without_claiming_a_skip(self, tmp_path: Path):
        result = _execute_step(tmp_path, self._STEP, 'python3() { echo "breaking=true"; }')
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "breaking=true"
        assert "maf-sandbox" in summary and "0.13.0" in summary
        # The skip rationale belongs to the work check now; this note only flags the release.
        assert "skipped" not in summary.lower()
        assert "273" not in summary

    def test_an_ordinary_release_stays_quiet(self, tmp_path: Path):
        result = _execute_step(tmp_path, self._STEP, 'python3() { echo "breaking=false"; }')
        assert result.returncode == 0, (
            "the step ended non-zero on the ordinary answer, which fails the release over a "
            f"context note: {result.stderr.decode('utf-8', 'replace')}"
        )
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "breaking=false"
        assert summary == "", "nothing was reported, so the summary has nothing to say"

    def test_a_detector_that_cannot_answer_does_not_fail_the_release(self, tmp_path: Path):
        result = _execute_step(
            tmp_path, self._STEP, 'python3() { echo "no section for 0.13.0" >&2; return 1; }'
        )
        assert result.returncode == 0, (
            "an unanswerable question must not fail the release: the dispatch is not this "
            f"step's to make: {result.stderr.decode('utf-8', 'replace')}"
        )
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "breaking=false"
        assert "::warning::" in result.stdout.decode("utf-8", "replace")
        assert summary == ""

    def test_the_detector_only_runs_for_a_real_core_release(self):
        """A dependent's own publish strands nothing above it, and a rehearsal stays frictionless."""
        condition = condition_after(PUBLISH_WORKFLOW, f"- name: {self._STEP}")
        assert "steps.resolve.outputs.package == 'maf-sandbox'" in condition
        assert "steps.resolve.outputs.target == 'pypi'" in condition


class TestTheBuildWorkCheckIsEarlyValidation:
    """The build run refuses a breaking core before a human is asked to approve, and records a
    provisional reading for the approver — but it no longer decides the dispatch.

    The dispatch verdict is the upload-time re-check's (#337): the `pypi` environment can hold
    while the index moves, so a `skip` reached at build can be stale by upload. This step's job is
    to fail the release on a break before the approval gate, and to say in the summary what the
    build-time reading was. It writes no `live_check` output for downstream jobs to gate on.
    """

    _STEP = "Verify the published dependents import against this core"

    def test_a_pass_writes_no_skip_summary(self, tmp_path: Path):
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { printf "every published dependent that admits maf-sandbox 0.13.0 '
            'imports against it (maf-sandbox-bicep==0.5.6)\\nlive_check=run\\n"; }',
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "", "the build step is early validation, not the dispatch"
        assert summary == "", "the live check runs, so the summary has nothing to report"

    def test_nothing_admitting_writes_a_provisional_summary(self, tmp_path: Path):
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { printf "no published dependent admits maf-sandbox 0.13.0; nothing to '
            'verify\\nlive_check=skip\\n"; }',
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "", "the build step is early validation, not the dispatch"
        assert "273" in summary, "a provisional skip has to point at the reason"
        assert "maf-sandbox" in summary and "0.13.0" in summary

    def test_a_break_fails_the_step(self, tmp_path: Path):
        # A break refuses the release before the dispatch is decided: `set -e` ends the step on
        # the script's exit 1, so no verdict is written and the gate is never reached.
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { echo "maf-sandbox-docker==0.2.0: ImportError" >&2; return 1; }',
        )
        assert result.returncode != 0, "a break must fail the step, not dispatch on a guess"
        output, _summary = _step_outputs(tmp_path)
        assert output.strip() == ""

    def test_the_work_check_only_runs_for_a_real_core_release(self):
        """A dependent's own publish strands nothing above it, and a rehearsal stays frictionless."""
        condition = condition_after(PUBLISH_WORKFLOW, f"- name: {self._STEP}")
        assert "steps.resolve.outputs.package == 'maf-sandbox'" in condition
        assert "steps.resolve.outputs.target == 'pypi'" in condition


class TestThePreUploadRecheckIsBreakRefusalOnly:
    """The pre-upload re-check refuses newly discovered breaks without deciding dispatch."""

    _STEP = "Re-verify only newly admitting published versions import against this core"

    def test_a_pass_writes_no_dispatch_output(self, tmp_path: Path):
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { printf "every published dependent newly admitting maf-sandbox 0.13.0 '
            'imports against it (maf-sandbox-bicep==0.5.6)\\nlive_check=run\\n"; }',
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "", "break refusal owns no dispatch output"
        assert summary == "", "the dispatch verdict is the post-upload step's, not this one's"

    def test_nothing_admitting_writes_no_dispatch_output(self, tmp_path: Path):
        # The provisional `skip` is not the dispatch verdict: a dependent can still admit during
        # the upload window, so this step forwards nothing and writes no summary.
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { printf "no published dependent admits maf-sandbox 0.13.0; nothing to '
            'verify\\nlive_check=skip\\n"; }',
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "", "the upload window is still open; this is not the dispatch"
        assert summary == "", "no provisional summary — the post-upload step writes the final one"

    def test_a_break_fails_the_step(self, tmp_path: Path):
        # A break refuses the upload before the core ships: `set -e` ends the step on the script's
        # exit 1, so no verdict is written and the upload is never reached.
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { echo "maf-sandbox-docker==0.7.0: ImportError" >&2; return 1; }',
        )
        assert result.returncode != 0, "a break must refuse the upload, not ship on a guess"
        output, _summary = _step_outputs(tmp_path)
        assert output.strip() == ""

    def test_the_recheck_only_runs_for_a_real_core_release(self):
        """A dependent's own publish strands nothing above it, and a rehearsal stays frictionless."""
        condition = condition_after(PUBLISH_WORKFLOW, f"- name: {self._STEP}")
        assert "needs.build.outputs.package == 'maf-sandbox'" in condition
        assert "needs.build.outputs.target == 'pypi'" in condition


class TestThePostUploadDispatchGatesTheLiveCheck:
    """The dispatch decision, read off the post-upload check in its own job.

    `run` dispatches the live check; `skip` suppresses it for the #273 ordering window; a break
    after the upload dispatches and surfaces red rather than refuses, since the upload is immutable
    (#443). The check lives outside `publish` (which holds `id-token: write`) because it imports
    newly-published dependent code.
    """

    _STEP = "Decide the live-check dispatch after the upload"
    _GATED_JOBS = ("wait-for-propagation:", "verify:")

    def test_a_pass_emits_run_and_no_skip_summary(self, tmp_path: Path):
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { printf "every published dependent newly admitting maf-sandbox 0.13.0 '
            'imports against it (maf-sandbox-bicep==0.5.6)\\nlive_check=run\\n"; }',
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "live_check=run"
        assert summary == "", "the live check runs, so the summary has nothing to report"

    def test_nothing_admitting_after_upload_emits_skip_and_says_why(self, tmp_path: Path):
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { printf "no published dependent admits maf-sandbox 0.13.0; nothing to '
            'verify\\nlive_check=skip\\n"; }',
        )
        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "live_check=skip"
        assert "273" in summary, "a skipped run has to point at the reason it was skipped"
        assert "maf-sandbox" in summary and "0.13.0" in summary

    def test_a_break_in_the_upload_window_dispatches_and_surfaces_red(self, tmp_path: Path):
        # `--dispatch` makes the script emit `live_check=run` to stdout and the break to stderr,
        # exit 0: the upload is immutable, so the live check is dispatched and the break surfaced
        # as `::error::` rather than the release refused (#443). The step must not fail — a failed
        # dispatch job would suppress the very live check it just decided to dispatch.
        result = _execute_step(
            tmp_path,
            self._STEP,
            'python3() { echo "maf-sandbox-docker==0.7.0: ImportError" >&2; '
            'printf "live_check=run\\n"; }',
        )
        assert result.returncode == 0, (
            "a break after the upload must dispatch, not fail the job and suppress the live check: "
            f"{result.stderr.decode('utf-8', 'replace')}"
        )
        output, summary = _step_outputs(tmp_path)
        assert output.strip() == "live_check=run", "an admitting dependent exists, so dispatch"
        stdout = result.stdout.decode("utf-8", "replace")
        assert "::error::maf-sandbox-docker==0.7.0" in stdout, (
            "the break has to surface as a red annotation, not ship silently"
        )
        assert "443" in summary, "a post-upload break has to point at the issue that owns the trade"
        assert "Release order" not in summary, "the refusal prose belongs to the pre-upload guard"

    def test_a_verdict_the_step_never_reached_still_dispatches(self):
        """A skipped step leaves the output empty, and empty has to mean "go".

        The dispatch only runs for a real core release, so every dependent's own publish reaches
        the gate with `live_check` unset. `!= 'skip'` is what makes that dispatch; `== 'run'`
        would silently stop the live check for every dependent release.
        """
        for job in self._GATED_JOBS:
            condition = condition_after(PUBLISH_WORKFLOW, job)
            assert "needs.dispatch.outputs.live_check != 'skip'" in condition, job

    def test_the_two_jobs_are_gated_alike(self):
        """`verify` needs `wait-for-propagation`, so a gate on one and not the other is a trap.

        Waiting for the index is only ever worth doing for a run that follows it. The two `if:`
        blocks are kept as one text so neither can drift into holding the other open.
        """
        conditions = {job: condition_after(PUBLISH_WORKFLOW, job) for job in self._GATED_JOBS}
        assert len(set(conditions.values())) == 1, conditions

    def test_the_dispatch_step_only_runs_for_a_real_core_release(self):
        """A dependent's own publish strands nothing above it, and a rehearsal stays frictionless."""
        condition = condition_after(PUBLISH_WORKFLOW, f"- name: {self._STEP}")
        assert "needs.build.outputs.package == 'maf-sandbox'" in condition
        assert "needs.build.outputs.target == 'pypi'" in condition

    def test_the_dispatch_check_runs_outside_the_publish_jobs_credential(self):
        """The check imports newly-published dependent code, so it must not hold `id-token: write`.

        `publish` mints the OIDC token that is the PyPI credential; running the import there would
        let a dependent that appeared in the upload window execute module-level code with that
        token in reach. The check lives in its own read-only job, and the publish job no longer
        runs it.
        """
        publish_block = _job_block(PUBLISH_WORKFLOW, "publish:")
        dispatch_block = _job_block(PUBLISH_WORKFLOW, "dispatch:")
        # The permissions key line at six spaces, not a comment that merely mentions it.
        credential = re.compile(r"^      id-token: write\s*$", re.MULTILINE)
        assert credential.search(publish_block), "the publishing credential stays with publish"
        assert not credential.search(dispatch_block), (
            "the dispatch check imports untrusted dependent code, so it must not hold id-token"
        )
        assert re.search(r"^      contents: read\s*$", dispatch_block, re.MULTILINE)
        assert f"- name: {self._STEP}" not in publish_block, (
            "the dispatch step must not run inside the publish job"
        )
        assert f"- name: {self._STEP}" in dispatch_block


def propagation_source() -> str:
    """The Python the propagation step runs, lifted out of its heredoc.

    Read from the workflow rather than duplicated here, so a change to the step is a change to
    what these tests exercise. A copy would go on passing after the step stopped matching it,
    which is the failure this whole module exists to prevent.
    """
    block = run_block(PUBLISH_WORKFLOW, "Poll PyPI until the just-published version is installable")
    opener = next(line for line in block.splitlines() if line.strip().endswith("<<'PY'"))
    body = block.split(opener, 1)[1]
    return body.split("\nPY", 1)[0]


class _Response:
    """The two attributes the step reads off `urlopen`, as a context manager."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status, self._body = status, body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def run_propagation(responses, *, package="maf-sandbox", version="0.20.0", settle_ticks=8):
    """Run the step against scripted responses; return its exit code and what it requested.

    ``responses`` is called with each requested URL and returns a `_Response`, an exception to
    raise, or bytes to serve as a 200. Time is driven rather than waited on: `sleep` advances a
    counter `monotonic` reads, so a settle window that takes a minute in the runner takes none
    here and the deadline is still reachable.
    """
    import os
    import time as real_time
    import urllib.request

    requested: list[str] = []
    clock = {"now": 0.0}

    def fake_urlopen(request, timeout=None):  # noqa: ARG001
        requested.append(request.full_url)
        answer = responses(request.full_url)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, _Response):
            return answer
        return _Response(200, answer)

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    def fake_monotonic() -> float:
        return clock["now"]

    # Patched on the real modules rather than injected into the namespace: the step's own first
    # line imports os, time and urllib, which rebinds anything seeded there.
    namespace: dict[str, object] = {"__name__": "__propagation__"}
    source = propagation_source()
    saved = (
        urllib.request.urlopen,
        real_time.monotonic,
        real_time.sleep,
        os.environ.get("PACKAGE"),
        os.environ.get("VERSION"),
    )
    urllib.request.urlopen = fake_urlopen
    real_time.monotonic = fake_monotonic
    real_time.sleep = fake_sleep
    os.environ["PACKAGE"], os.environ["VERSION"] = package, version
    try:
        code = 0
        try:
            exec(compile(source, "<propagation>", "exec"), namespace)  # noqa: S102
        except SystemExit as exit_called:
            code = exit_called.code if isinstance(exit_called.code, int) else 1
    finally:
        urllib.request.urlopen, real_time.monotonic, real_time.sleep = saved[:3]
        for name, previous in (("PACKAGE", saved[3]), ("VERSION", saved[4])):
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous
    return code, requested


def _listing(*filenames: str, version: str = "0.20.0") -> bytes:
    files = [
        {"filename": name, "url": f"https://files.pythonhosted.org/x/{name}#sha256=deadbeef"}
        for name in filenames
    ]
    return json.dumps({"versions": [version], "files": files}).encode("utf-8")


_WHEEL = "maf_sandbox-0.20.0-py3-none-any.whl"
_SDIST = "maf_sandbox-0.20.0.tar.gz"


class TestThePropagationPoll:
    """What has to hold before verify is released to resolve the version just published.

    Driven through the step's own source with scripted responses, because every failure here is
    one that only appears against a real index mid-propagation — the state that is hardest to
    reproduce at the moment it matters and impossible to reproduce afterwards.
    """

    def test_a_complete_listing_releases_verify(self):
        code, _ = run_propagation(lambda url: _listing(_WHEEL, _SDIST))
        assert code == 0

    def test_the_cache_buster_reaches_the_server_on_an_artifact_url(self):
        """An artifact URL ends in `#sha256=…`, and a query after it is inside the fragment.

        The client strips the fragment before the request, so the server would see the same
        URL every poll and answer from this edge's warm cache — the one thing the busting is
        there to prevent, silently not happening.
        """
        _, requested = run_propagation(lambda url: _listing(_WHEEL, _SDIST))
        artifact = [url for url in requested if ".whl" in url]
        assert artifact, requested
        split = urllib.parse.urlsplit(artifact[0])
        assert "_cb=" in split.query, artifact[0]
        assert split.fragment.startswith("sha256="), artifact[0]
        assert "_cb=" not in split.fragment, artifact[0]

    @pytest.mark.parametrize(("present", "missing"), [((_WHEEL,), "sdist"), ((_SDIST,), "wheel")])
    def test_one_artifact_kind_alone_does_not_pass(self, present, missing):
        """They upload as one publish and appear independently.

        A resolver that finds only the sdist builds from source where it should have taken the
        wheel, so counting a non-empty file list passes on whichever landed first.
        """
        assert missing in {"wheel", "sdist"}
        code, _ = run_propagation(lambda url: _listing(*present))
        assert code == 1

    def test_unparseable_json_resets_the_window_rather_than_ending_the_job(self):
        """A 200 carrying truncated JSON is an edge mid-write — the state this poll waits out.

        Raising `JSONDecodeError` out of the step would end the release on the symptom it is
        supposed to tolerate, and the run would report a propagation failure that never had a
        chance to resolve.
        """
        seen = {"n": 0}

        def responses(url):
            if url.startswith("https://pypi.org/simple/"):
                seen["n"] += 1
                if seen["n"] == 1:
                    return b'{"versions": ["0.20.'  # truncated mid-write
                return _listing(_WHEEL, _SDIST)
            return b""

        code, _ = run_propagation(responses)
        assert code == 0
        assert seen["n"] > 1, "the poll gave up on the first bad body instead of retrying"

    def test_an_unreachable_index_is_recoverable_too(self):
        seen = {"n": 0}

        def responses(url):
            if url.startswith("https://pypi.org/simple/"):
                seen["n"] += 1
                if seen["n"] == 1:
                    return urllib.error.URLError("connection reset")
                return _listing(_WHEEL, _SDIST)
            return b""

        code, _ = run_propagation(responses)
        assert code == 0

    def test_a_flicker_restarts_the_settle_window(self):
        """A single good poll is not enough: an edge mid-purge answers once and then does not.

        Releasing verify on the first success is what the settle window exists to refuse, so a
        listing that regresses has to push the deadline out rather than count toward it.
        """
        seen = {"n": 0}

        def responses(url):
            if url.startswith("https://pypi.org/simple/"):
                seen["n"] += 1
                return _listing(_WHEEL, _SDIST) if seen["n"] != 2 else _listing(_WHEEL)
            return b""

        code, _ = run_propagation(responses)
        assert code == 0
        # Settling is 60s at 15s a tick, so four clean polls in a row. The regression at poll 2
        # means it cannot have finished on the four that started before it.
        assert seen["n"] >= 6, seen["n"]

    def test_a_version_that_never_lists_fails_the_deadline(self):
        code, _ = run_propagation(lambda url: json.dumps({"versions": [], "files": []}).encode())
        assert code == 1

    def test_an_artifact_that_never_serves_fails_the_deadline(self):
        def responses(url):
            if url.startswith("https://pypi.org/simple/"):
                return _listing(_WHEEL, _SDIST)
            return _Response(404, b"")

        code, _ = run_propagation(responses)
        assert code == 1
