"""Repository-level release wiring: every package registered, everywhere, consistently.

These are not any one package's tests — they are about the four files that have to agree for
a release to happen at all (`release-please-config.json`, `.release-please-manifest.json`,
`publish-packages.yml` and `pr-title.yml`), which is why they live at the root rather than
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
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "release-please-config.json"
MANIFEST_PATH = REPO_ROOT / ".release-please-manifest.json"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PUBLISH_WORKFLOW = WORKFLOWS / "publish-packages.yml"
PR_TITLE_WORKFLOW = WORKFLOWS / "pr-title.yml"

CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

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


class TestComponentMatchesDistributionName:
    """The tag's component is configuration, and nothing derives it from the package.

    So it can drift from the name it is supposed to mirror — silently, because release-please
    would go on tagging happily under the wrong component while the publish workflow, which
    maps a tag back to a directory, listens for the right one and never fires.
    """

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_configured_component_is_the_distribution_name(self, package_path: str):
        assert configured_component(package_path) == declared_name(package_path)


class TestTagsResolveToExactlyOnePackage:
    """`maf-sandbox-v*` must not also swallow `maf-sandbox-aca-v0.1.0`.

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


class TestReleasesAreDraftedNotAnnounced:
    """The ordering property #12 established, now enforced by configuration.

    A draft carries no tag and no notification, so the GitHub Release only becomes real once
    `publish-packages.yml` flips it — after the upload to PyPI succeeded. Dropping `draft`
    would silently restore the thing that job exists to prevent: a Release announcing a
    version that PyPI does not have.
    """

    def test_draft_is_on(self):
        assert CONFIG["draft"] is True

    def test_component_is_in_the_tag(self):
        # Without this, all three packages would tag as plain `v<version>` and collide.
        assert CONFIG["include-component-in-tag"] is True
