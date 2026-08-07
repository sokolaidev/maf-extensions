"""Repository-level release wiring: every package registered, everywhere, consistently.

These are not any one package's tests — they are about the three files that have to agree
for a release to happen at all (`release-please-config.json`, `.release-please-manifest.json`
and `publish-packages.yml`), which is why they live at the root rather than under a package.

Each failure here is one that is otherwise silent: a new package that release-please never
proposes a release for, a manifest that has drifted from the version actually declared, or
two packages whose tags collide. None of those break a test, a type check or a build — they
break a release, at the one moment when the thing that went wrong is hardest to undo.
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
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-packages.yml"

CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

# Every directory under packages/ that is actually a distribution.
PACKAGE_PATHS = sorted(
    str(path.parent.relative_to(REPO_ROOT)).replace("\\", "/")
    for path in REPO_ROOT.glob("packages/*/pyproject.toml")
)


def declared_version(package_path: str) -> str:
    pyproject = tomllib.loads(
        (REPO_ROOT / package_path / "pyproject.toml").read_text("utf-8")
    )
    return pyproject["project"]["version"]


def declared_name(package_path: str) -> str:
    pyproject = tomllib.loads(
        (REPO_ROOT / package_path / "pyproject.toml").read_text("utf-8")
    )
    return pyproject["project"]["name"]


def publish_tag_globs() -> list[str]:
    """The `on.push.tags` globs, read out of the workflow without a YAML dependency."""
    workflow = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"^ *tags:\n((?: *- *\"[^\"]+\"\n)+)", workflow, re.MULTILINE)
    assert block is not None, (
        f"no `on.push.tags` block found in {PUBLISH_WORKFLOW.name}"
    )
    return re.findall(r"\"([^\"]+)\"", block.group(1))


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


class TestTagsResolveToExactlyOnePackage:
    """`maf-sandbox-v*` must not also swallow `maf-sandbox-aca-v0.1.0`.

    It does not, because the character after `maf-sandbox-` there is `a` rather than `v` —
    but that is a property of these particular names, not of the scheme, so a fourth package
    could quietly break it. Two globs matching one tag means two publish runs for one release.
    """

    @pytest.mark.parametrize("package_path", PACKAGE_PATHS)
    def test_each_package_tag_matches_exactly_one_glob(self, package_path: str):
        # The tag release-please will produce: component (the distribution name), then -v.
        tag = f"{declared_name(package_path)}-v{declared_version(package_path)}"
        matched = [
            glob for glob in publish_tag_globs() if fnmatch.fnmatchcase(tag, glob)
        ]
        assert matched == [f"{declared_name(package_path)}-v*"], (
            f"tag {tag} matched {matched}"
        )

    def test_no_glob_is_orphaned(self):
        tags = [
            f"{declared_name(path)}-v{declared_version(path)}" for path in PACKAGE_PATHS
        ]
        for glob in publish_tag_globs():
            assert any(fnmatch.fnmatchcase(tag, glob) for tag in tags), (
                f"glob {glob} matches no package — a rename left it behind"
            )


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
