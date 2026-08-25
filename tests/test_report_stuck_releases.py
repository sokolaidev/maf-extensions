"""The stuck-release report: what it decides, what it writes, and how `release-please.yml` wires it.

A merged Release PR release-please could not finish stops every package's release, and the run
that noticed is red on `main` where nobody reads it. So two things are pinned here — that the
decision is right, and that the step carrying it out runs at all. The second is the one with
no other guard: a step that stops running fails silently, exactly like the failure it reports.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "report_stuck_releases.py"
_spec = importlib.util.spec_from_file_location("report_stuck_releases", _SCRIPT)
assert _spec and _spec.loader
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)

_WORKFLOW = yaml.safe_load(
    (_ROOT / ".github" / "workflows" / "release-please.yml").read_text(encoding="utf-8")
)
_STEPS = _WORKFLOW["jobs"]["prepare"]["steps"]

#: A minute after the fixture merged, so the fixture is a release this run should have made.
_STARTED = "2026-08-24T20:34:00Z"

_ACAS = {
    "number": 624,
    "title": "chore(main): release maf-sandbox-acas 0.13.0",
    "url": "https://github.com/sokolaidev/maf-extensions/pull/624",
    "mergedAt": "2026-08-24T20:33:07Z",
    "mergeCommit": {"oid": "ae818cc"},
}


def _document(**overrides: object) -> dict:
    document = {
        "run_started_at": _STARTED,
        "run_url": "https://github.com/sokolaidev/maf-extensions/actions/runs/1",
        "pending": [dict(_ACAS)],
        "issue": None,
    }
    document.update(overrides)
    return document


def _step_named(name: str) -> dict:
    for step in _STEPS:
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"no step in release-please.yml is named {name!r}. Nothing else reports a release "
        "left stuck, so removing it takes the signal with it."
    )


class TestTheTagIsReadOffTheReleasePrTitle:
    """The tag is what the recovery creates, and a maintainer under pressure should not guess it."""

    def test_a_single_package_release_names_its_tag(self):
        assert report.tag_for(_ACAS["title"]) == "maf-sandbox-acas-v0.13.0"

    def test_the_component_prefix_is_not_confused_with_the_core(self):
        """`maf-sandbox-acas` and `maf-sandbox` share a prefix; the tag must not."""
        assert report.tag_for("chore(main): release maf-sandbox 0.23.0") == "maf-sandbox-v0.23.0"

    def test_a_title_naming_no_version_says_so_rather_than_guessing(self):
        assert report.tag_for("chore(main): release main") is None

    def test_the_body_says_where_to_find_the_tag_when_it_cannot_read_one(self):
        stuck = [{**_ACAS, "title": "chore(main): release main"}]
        text = report.body(stuck, "")
        assert "manifest entry" in text
        assert "gh release create" not in text


class TestOnlyAReleaseThisRunOwedCounts:
    def test_a_release_merged_before_the_run_started_is_stuck(self):
        assert report.stuck_releases([dict(_ACAS)], _STARTED) == [_ACAS]

    def test_a_release_merged_while_the_run_was_in_flight_is_not(self):
        """It belongs to the run its own merge triggered, which is queued behind this one.

        Without this the step would open a tracking issue seconds before the next run releases
        the thing and closes it again — a false alarm on the one signal meant to be trusted.
        """
        later = {**_ACAS, "number": 700, "mergedAt": "2026-08-24T20:36:00Z"}
        assert report.stuck_releases([later], _STARTED) == []

    def test_a_merge_at_the_moment_the_run_started_counts(self):
        same = {**_ACAS, "mergedAt": _STARTED}
        assert report.stuck_releases([same], _STARTED) == [same]

    @pytest.mark.parametrize("merged", [None, "", "not a timestamp"])
    def test_a_merge_time_github_did_not_report_counts(self, merged: str | None):
        """Missing the wedge is the failure this exists to prevent; a false alarm self-closes."""
        unknown = {**_ACAS, "mergedAt": merged}
        assert report.stuck_releases([unknown], _STARTED) == [unknown]

    def test_the_oldest_merge_comes_first(self):
        """release-please stops at the oldest unfinished one, so that is what to clear first."""
        older = {**_ACAS, "number": 600, "mergedAt": "2026-08-20T09:00:00Z"}
        assert [pr["number"] for pr in report.stuck_releases([dict(_ACAS), older], _STARTED)] == [
            600,
            624,
        ]


class TestThePlanIsOneIssue:
    def test_a_wedge_with_no_tracker_opens_one(self):
        plan = report.plan(_document())
        assert plan["action"] == "open"
        assert plan["title"] == report.TITLE
        assert report.MARKER in plan["body"]

    def test_a_wedge_with_a_tracker_updates_that_one(self):
        """Updated rather than duplicated: a run happens on every push to `main`."""
        plan = report.plan(_document(issue={"number": 777}))
        assert plan["action"] == "update"
        assert plan["issue"] == 777

    def test_a_cleared_train_closes_the_tracker(self):
        plan = report.plan(_document(pending=[], issue={"number": 777}))
        assert plan["action"] == "close"
        assert plan["issue"] == 777
        assert plan["comment"]

    def test_a_clear_train_with_no_tracker_does_nothing(self):
        assert report.plan(_document(pending=[], issue=None))["action"] == "none"

    def test_a_release_this_run_did_not_owe_leaves_the_train_clear(self):
        """The filter has to reach the plan, not only `stuck_releases`."""
        later = {**_ACAS, "mergedAt": "2026-08-24T20:36:00Z"}
        assert report.plan(_document(pending=[later]))["action"] == "none"


class TestTheBodyCarriesTheRecovery:
    """Three steps, and the middle one is the step nobody would guess."""

    @pytest.fixture
    def text(self) -> str:
        return report.plan(_document())["body"]

    def test_it_names_the_stuck_pull_request(self, text: str):
        assert "#624" in text
        assert _ACAS["url"] in text

    def test_it_names_the_tag_and_the_commit_to_create_it_at(self, text: str):
        assert "gh release create maf-sandbox-acas-v0.13.0 --target ae818cc" in text

    def test_it_names_the_changelog_section_the_notes_come_from(self, text: str):
        assert "packages/maf-sandbox-acas/CHANGELOG.md" in text

    def test_it_flips_the_label(self, text: str):
        """Without the flip release-please retries the same release for ever."""
        assert 'gh pr edit 624 --remove-label "autorelease: pending"' in text
        assert '--add-label "autorelease: tagged"' in text

    def test_it_dispatches_the_publish_rather_than_leaving_it_to_the_tag(self, text: str):
        """A tag created by a user token starts no workflow, so nothing would upload."""
        assert "gh workflow run publish-packages.yml --ref maf-sandbox-acas-v0.13.0" in text

    def test_it_links_the_run_that_noticed(self, text: str):
        assert "actions/runs/1" in text

    def test_it_says_the_issue_closes_itself(self, text: str):
        assert "closes itself" in text


class TestTheMarkerHasOneDefinition:
    def test_the_script_prints_it_for_the_workflow_to_search_on(
        self, capsys: pytest.CaptureFixture
    ):
        assert report.main(["report_stuck_releases.py", "--marker"]) == 0
        assert capsys.readouterr().out.strip() == report.MARKER

    def test_the_workflow_asks_the_script_rather_than_repeating_it(self):
        """A second copy in the YAML would drift and open a second issue every run."""
        step = _step_named("Name any release this run left stuck")
        assert "report_stuck_releases.py --marker" in step["run"]
        assert report.MARKER not in step["run"]


class TestTheStepRunsWhateverElseHappened:
    def test_it_runs_when_release_please_failed(self):
        """The wedge *is* a failed release-please, so `success()` would never report one."""
        step = _step_named("Name any release this run left stuck")
        assert "always()" in str(step.get("if", ""))

    def test_it_is_the_last_step(self):
        """It reads what the run left behind, so anything after it would not be counted."""
        assert _STEPS[-1].get("name") == "Name any release this run left stuck"

    def test_the_checkout_is_first_and_unconditional(self):
        """The step reads `scripts/` off disk, and it runs on runs that release nothing."""
        assert _STEPS[0]["name"] == "Check out"
        assert "if" not in _STEPS[0]
        assert "actions/checkout@" in _STEPS[0]["uses"]

    def test_only_one_step_checks_out(self):
        """Two checkouts of the same ref is the shape left behind by moving the first one."""
        checkouts = [step for step in _STEPS if "actions/checkout@" in str(step.get("uses", ""))]
        assert len(checkouts) == 1

    @pytest.mark.parametrize("permission", ["issues", "pull-requests", "actions", "contents"])
    def test_the_job_can_still_reach_what_the_step_reads(self, permission: str):
        """Each one is a 403 the step cannot report, because reporting is what it was doing."""
        assert _WORKFLOW["jobs"]["prepare"]["permissions"][permission] == "write"


class TestTheRenderedChangelogPathIsReal:
    def test_every_package_directory_is_named_after_its_package(self):
        """`_recovery` builds `packages/<package-name>/CHANGELOG.md` out of the PR title."""
        config = json.loads((_ROOT / "release-please-config.json").read_text(encoding="utf-8"))
        mismatched = [
            path
            for path, entry in config["packages"].items()
            if Path(path).name != entry["package-name"]
        ]
        assert not mismatched, (
            f"{mismatched} do not sit in a directory named after the package, so the recovery "
            "steps in the tracking issue would name a CHANGELOG.md that is not there"
        )
