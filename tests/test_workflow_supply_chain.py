"""Every action is pinned to a commit Dependabot can move, and only a pushing job keeps its token."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOWS = sorted((_ROOT / ".github" / "workflows").glob("*.y*ml"))
_DEPENDABOT = _ROOT / ".github" / "dependabot.yml"

#: The jobs that push with the token `actions/checkout` leaves in `.git/config`.
_PUSHING = {("release-please.yml", "prepare"), ("bicep-catalog.yml", "propose")}

_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$")
_PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
# Dependabot rewrites the comment only when it ends with a version tagged at the pinned commit.
_VERSION_COMMENT = re.compile(r"\s+# v\d+(\.\d+)*")


def _jobs():
    for path in _WORKFLOWS:
        for name, job in yaml.safe_load(path.read_text("utf-8"))["jobs"].items():
            yield path.name, name, job


def _pushes(job: dict) -> bool:
    return any(
        "git push" in step.get("run", "")
        or step.get("uses", "").startswith("peter-evans/create-pull-request@")
        for step in job.get("steps", [])
    )


@pytest.mark.parametrize("path", _WORKFLOWS, ids=lambda path: path.name)
def test_every_action_is_a_commit_with_its_version_beside_it(path: Path):
    uses = [
        (number, match)
        for number, line in enumerate(path.read_text("utf-8").splitlines(), 1)
        if (match := _USES.match(line)) and not match.group(1).startswith("./")
    ]
    assert uses, f"{path.name} names no action"
    unpinned = [
        f"{path.name}:{number}: {match.group(0).strip()}"
        for number, match in uses
        if not (_PINNED.fullmatch(match.group(1)) and _VERSION_COMMENT.fullmatch(match.group(2)))
    ]
    assert unpinned == []


def test_a_checkout_keeps_no_token_unless_its_job_pushes():
    wrong = [
        f"{workflow}:{name}"
        for workflow, name, job in _jobs()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("actions/checkout@")
        and (step.get("with", {}).get("persist-credentials") is not False)
        != ((workflow, name) in _PUSHING)
    ]
    assert wrong == []


def test_the_jobs_that_keep_it_are_the_ones_that_push():
    assert {(workflow, name) for workflow, name, job in _jobs() if _pushes(job)} == _PUSHING


def test_dependabot_moves_the_action_pins_under_a_title_that_releases_nothing():
    config = yaml.safe_load(_DEPENDABOT.read_text("utf-8"))
    updates = [
        entry for entry in config["updates"] if entry["package-ecosystem"] == "github-actions"
    ]
    assert len(updates) == 1, f"expected one github-actions update, got {updates}"
    assert updates[0]["directory"] == "/"
    assert updates[0]["commit-message"]["prefix"] == "ci"


def test_no_dependabot_group_is_limited_by_update_type():
    # Such a group takes every dependency its patterns match, proposing none of the updates it
    # excludes (dependabot-core#14202).
    config = yaml.safe_load(_DEPENDABOT.read_text("utf-8"))
    limited = [
        f"{entry['package-ecosystem']}:{name}"
        for entry in config["updates"]
        for name, group in entry.get("groups", {}).items()
        if "update-types" in group
    ]
    assert limited == []
