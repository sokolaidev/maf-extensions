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

#: What each configured package has released, in the shape the workflow passes. The package
#: names are this repository's and `TestTheFixtureIsThisRepository` holds them to the
#: configuration, so this cannot quietly cover fewer than the workflow does. The versions are
#: the fixture's own: the manifest moves on every release, so a copy of it here would turn
#: this suite red whenever anybody ships.
_RELEASES = {
    "maf-sandbox": "0.23.1",
    "maf-sandbox-acas": "0.13.0",
    "maf-sandbox-bicep": "0.9.6",
    "maf-sandbox-codeact": "0.7.4",
    "maf-sandbox-docker": "0.8.1",
    "maf-sandbox-wslc": "0.11.2",
}


def _releasing(version: str, package: str = "maf-sandbox") -> dict[str, str]:
    """A manifest that records exactly ``version``, so only the grammar can refuse it."""
    return {package: version}


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
        "releases": dict(_RELEASES),
    }
    document.update(overrides)
    return document


def _step_index(name: str) -> int:
    """Where a step sits in the job, so an ordering constraint can be stated as one."""
    for index, step in enumerate(_STEPS):
        if step.get("name") == name:
            return index
    raise AssertionError(f"no step in release-please.yml is named {name!r}")


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
        assert report.tag_for(_ACAS["title"], _RELEASES) == "maf-sandbox-acas-v0.13.0"

    def test_the_component_prefix_is_not_confused_with_the_core(self):
        """`maf-sandbox-acas` and `maf-sandbox` share a prefix; the tag must not."""
        assert (
            report.tag_for("chore(main): release maf-sandbox 0.23.1", _RELEASES)
            == "maf-sandbox-v0.23.1"
        )

    def test_a_title_naming_no_version_says_so_rather_than_guessing(self):
        assert report.tag_for("chore(main): release main", _RELEASES) is None

    @pytest.mark.parametrize(
        "title",
        [
            "do not release maf-sandbox 0.23.0",
            "revert: release maf-sandbox 0.23.0",
            "chore(main): release maf-sandbox 0.23.0 (reverted)",
        ],
    )
    def test_only_release_pleases_own_title_names_a_tag(self, title: str):
        """Matched end to end. A substring match reads a sentence *about* a release as one.

        The manifest records the version each of these names, so the anchoring is the only
        thing left that can refuse them.
        """
        assert report.tag_for(title, _releasing("0.23.0")) is None

    @pytest.mark.parametrize("version", ["1.0.0..9", "1.0.0.", "1.0.0-", "1.0.0+"])
    def test_a_version_git_would_refuse_as_a_ref_names_no_tag(self, version: str):
        """`..` and a trailing `.` are not legal in a ref, so the tag would not be creatable."""
        assert (
            report.tag_for(f"chore(main): release maf-sandbox {version}", _releasing(version))
            is None
        )

    def test_the_version_is_not_read_back_out_of_the_composed_tag(self):
        """A version may itself contain `-v`, and `rsplit("-v", 1)` then splits in the wrong place.

        `maf-sandbox-v1.0.0-v1` decomposes to package `maf-sandbox-v1.0.0` and version `1`,
        which would dispatch the publish for a package that does not exist.
        """
        assert report.release_of(
            "chore(main): release maf-sandbox 1.0.0-v1", _releasing("1.0.0-v1")
        ) == (
            "maf-sandbox",
            "1.0.0-v1",
        )
        text = report.body(
            [{**_ACAS, "title": "chore(main): release maf-sandbox 1.0.0-v1"}],
            "",
            released_tags=[],
            releases=_releasing("1.0.0-v1"),
        )
        assert "-f package=maf-sandbox " in text
        assert "--title 'maf-sandbox 1.0.0-v1'" in text

    @pytest.mark.parametrize(
        "version",
        ["0.1.0;whoami", "0.1.0$(whoami)", "0.1.0`whoami`", "0.1.0&&whoami", "0.1.0|whoami"],
    )
    def test_a_version_carrying_shell_syntax_names_no_tag(self, version: str):
        """The tag is rendered into commands a maintainer copies into a shell and runs.

        A pull request title is editable, so what the tag is built from is held to the
        characters a git tag may carry — refusing to name a tag is the safe answer, and the
        issue then says where to find it instead.
        """
        assert (
            report.tag_for(f"chore(main): release maf-sandbox {version}", _releasing(version))
            is None
        )

    @pytest.mark.parametrize("suffix", ["-rc.1", "+build.7", "-alpha1"])
    def test_a_prerelease_or_build_version_still_names_its_tag(self, suffix: str):
        """Narrower than `\\S*`, still wide enough for every version release-please cuts."""
        tag = report.tag_for(
            f"chore(main): release maf-sandbox 1.0.0{suffix}", _releasing(f"1.0.0{suffix}")
        )
        assert tag == f"maf-sandbox-v1.0.0{suffix}"

    def test_the_body_says_where_to_find_the_tag_when_it_cannot_read_one(self):
        stuck = [{**_ACAS, "title": "chore(main): release main"}]
        text = report.body(stuck, "", released_tags=[], releases=_RELEASES)
        assert ".release-please-manifest.json" in text
        assert "gh release create" not in text

    def test_it_names_the_order_rather_than_offering_the_one_command_it_could(self):
        """The label flip is the only command here that needs no tag, and the worst to offer.

        Run before the Release exists it tells release-please the version was released, and
        that number is then spent on a release that was never tagged or published. So the
        unreadable-title path names the order instead of rendering the command it could.
        """
        stuck = [{**_ACAS, "title": "chore(main): release main"}]
        text = report.body(stuck, "", released_tags=[], releases=_RELEASES)
        assert "gh pr edit" not in text
        assert "in that order" in text.replace("**", "")
        assert "cannot be reused" in text
        # And it points somewhere: naming the obstacle without the next step is what leaves a
        # maintainer reconstructing the commands from memory, mid-incident.
        assert ".release-please-manifest.json" in text
        # Without restating how many there are. Two places counting the same commands drift
        # apart, and this is the path taken when nothing else about the title can be trusted.
        assert not [n for n in ("two steps", "three steps", "four steps") if n in text]
        # Twice: once here, once in the closing paragraph every body carries. Counted rather
        # than merely present, because that closing line would satisfy a bare `in` on its own
        # and the pointer could go missing from the paragraph that needs it.
        assert text.count("docs/maintainers.md") == 2
        assert (
            report.body([_ACAS], "", released_tags=[], releases=_RELEASES).count(
                "docs/maintainers.md"
            )
            == 1
        )


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


class TestARunStartNobodyCanReadFiltersNothing:
    """The same unreadable timestamp has to fail in opposite directions on the two sides.

    On a merge time the far past means "old enough to be stuck", so the release is reported.
    On the run start that same value means "after nothing", which drops every pending release
    and reports none — the silent miss this whole step exists to prevent. `gh api --jq` prints
    the string `null` for a field the API did not return, so it is an ordinary absence rather
    than anything exotic that gets here.
    """

    _LATER = {**_ACAS, "number": 700, "mergedAt": "2026-08-24T20:36:00Z"}

    @pytest.mark.parametrize("started", ["", "null", "not a timestamp", "2026-13-45"])
    def test_every_pending_release_is_still_reported(self, started: str):
        stuck = report.stuck_releases([dict(_ACAS), dict(self._LATER)], started)
        assert [pr["number"] for pr in stuck] == [624, 700]

    @pytest.mark.parametrize("started", ["", "null"])
    def test_so_the_tracker_is_opened_rather_than_never_raised(self, started: str):
        assert report.plan(_document(run_started_at=started))["action"] == "open"

    def test_a_merge_time_nobody_can_read_either_is_still_reported(self):
        """Both sides unreadable at once, which is the state a bad API response leaves."""
        unknown = {**_ACAS, "mergedAt": None}
        assert report.stuck_releases([unknown], "null") == [unknown]

    def test_and_a_readable_run_start_still_filters(self):
        """The safety net must not swallow the filter it is a net for."""
        assert report.stuck_releases([dict(self._LATER)], _STARTED) == []


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


class TestTheFixtureIsThisRepository:
    """`_RELEASES` covers what the workflow passes, so a fixture that drifted would test less.

    The names, and not the versions. What each package has released is checked against the
    manifest at run time; pinning a copy of the manifest *here* would fail on every release of
    every package, and a suite that goes red because somebody else shipped is measuring
    something other than the code in front of it.
    """

    def test_it_names_every_configured_package(self):
        config = json.loads((_ROOT / "release-please-config.json").read_text(encoding="utf-8"))
        assert set(_RELEASES) == {entry["package-name"] for entry in config["packages"].values()}


class TestAVersionTheManifestDoesNotRecord:
    """Configured is not released. A merged Release PR's title is editable; the manifest is not.

    A Release PR bumps `.release-please-manifest.json` as part of its own diff, so once merged
    it holds exactly the version that pull request released.
    """

    @pytest.mark.parametrize("version", ["999.0.0", "0.13.1", "0.12.2"])
    def test_it_names_no_tag(self, version: str):
        assert report.tag_for(f"chore(main): release maf-sandbox-acas {version}", _RELEASES) is None

    def test_the_recovery_is_withheld_rather_than_aimed_at_an_invented_release(self):
        """Aimed anyway it tags a version nobody released and flips the label on the real
        pull request, spending the version release-please still owes."""
        stuck = [{**_ACAS, "title": "chore(main): release maf-sandbox-acas 999.0.0"}]
        text = report.body(stuck, "", released_tags=[], releases=_RELEASES)
        assert "gh release create" not in text
        assert "gh pr edit" not in text
        assert ".release-please-manifest.json" in text


class TestClosingTakesMoreEvidenceThanReporting:
    """Closing is the only action that removes the signal, so it asks for more than the filter.

    A release merged too recently for this run to have owed it is still a release nobody has
    made, and the run that will own it has not had its turn.
    """

    _LATER = {**_ACAS, "number": 700, "mergedAt": "2026-08-24T20:36:00Z"}

    def test_a_cleared_train_still_closes(self):
        assert report.plan(_document(pending=[], issue={"number": 777}))["action"] == "close"

    def test_a_merge_this_run_did_not_owe_holds_the_tracker_open(self):
        plan = report.plan(_document(pending=[dict(self._LATER)], issue={"number": 777}))
        assert plan["action"] == "none"

    def test_and_the_summary_says_why_rather_than_reading_as_all_clear(self):
        plan = report.plan(_document(pending=[dict(self._LATER)], issue={"number": 777}))
        assert "holding the tracker open" in plan["summary"]

    def test_with_no_tracker_open_there_is_nothing_to_hold(self):
        plan = report.plan(_document(pending=[dict(self._LATER)], issue=None))
        assert plan["action"] == "none"
        assert "no merged Release PR" in plan["summary"]


class TestOnlyAReleaseThisRepositoryMakesGetsARecovery:
    """Ref-safe is not the same as real, and a merged Release PR's title is editable."""

    def test_a_package_this_repository_does_not_have_names_no_tag(self):
        assert report.tag_for("chore(main): release not-a-package 1.0.0", _RELEASES) is None

    def test_the_recovery_is_withheld_rather_than_built_from_a_guess(self):
        """Built anyway it names a changelog that is not there, tags a name nobody publishes,
        and flips the label on the real pull request before the dispatch fails."""
        stuck = [{**_ACAS, "title": "chore(main): release not-a-package 1.0.0"}]
        text = report.body(stuck, "", released_tags=[], releases=_RELEASES)
        assert "not-a-package" in text
        assert "gh release create" not in text
        assert "gh pr edit" not in text

    def test_an_empty_package_list_releases_nothing(self):
        """A caller that sends none must withhold the recovery, never widen it."""
        assert report.tag_for(_ACAS["title"], {}) is None

    @pytest.mark.parametrize("version", ["1.0.0+build.lock", "1.0.0-rc.lock"])
    def test_a_version_composing_a_tag_git_refuses_names_no_tag(self, version: str):
        """git rejects a ref ending in `.lock`, so that tag could never have been created."""
        assert (
            report.tag_for(f"chore(main): release maf-sandbox {version}", _releasing(version))
            is None
        )


class TestTheRenderedBlockStopsOnAFailure:
    """Pasted as a block, an unchained sequence runs the label flip after a failed create."""

    @staticmethod
    def _shell(text: str) -> str:
        return TestNothingInTheRenderedCommandsExpands._shell(text)

    def test_every_command_but_the_last_is_chained(self):
        lines = self._shell(report.plan(_document())["body"]).split("\n")
        assert [line for line in lines if line.endswith(" &&")]
        assert not lines[-1].endswith("&&")

    def test_the_notes_file_is_checked_before_the_release_is_created(self):
        """A pipeline's status is awk's, and awk succeeds on an empty stream."""
        shell = self._shell(report.plan(_document())["body"])
        assert "[ -s notes.md ] &&" in shell
        assert shell.index("[ -s notes.md ]") < shell.index("gh release create")

    def test_the_already_created_path_is_chained_too(self):
        plan = report.plan(_document(released_tags=["maf-sandbox-acas-v0.13.0"]))
        assert self._shell(plan["body"]).split("\n")[1].endswith(" &&")


class TestNothingInTheRenderedCommandsExpands:
    """What the issue prints is copy-pasted into a shell holding a maintainer's credentials."""

    @staticmethod
    def _shell(text: str) -> str:
        """Everything inside the body's fenced ```bash blocks, joined."""
        blocks, inside = [], False
        for line in text.split("\n"):
            if line.startswith("```bash"):
                inside = True
            elif line.startswith("```"):
                inside = False
            elif inside:
                blocks.append(line)
        assert blocks, "the recovery rendered no shell at all"
        return "\n".join(blocks)

    @pytest.mark.parametrize("metacharacter", ["$", "`", ";", "<", "(", ")"])
    def test_no_metacharacter_reaches_the_shell(self, metacharacter: str):
        """`|`, `>` and `&` are absent from this list because the template itself uses them.

        The notes extraction is a pipeline into a file, so their presence says nothing. What
        keeps an interpolated one out is the grammar the title is matched against, and the
        rendered block below, which is asserted whole.
        """
        shell = self._shell(report.plan(_document())["body"])
        assert metacharacter not in shell

    def test_the_rendered_block_is_exactly_this(self):
        """Asserted whole, because every line of it is copied into a shell and run.

        A looser check passes on a command that silently changed shape; this one makes any
        change to what is interpolated show up as a diff somebody has to look at.
        """
        assert self._shell(report.plan(_document())["body"]).split("\n") == [
            "git show ae818cc:packages/maf-sandbox-acas/CHANGELOG.md \\",
            "  | awk '/^## \\[/{n++} n==1' > notes.md &&",
            "[ -s notes.md ] &&",
            "gh release create maf-sandbox-acas-v0.13.0 --target ae818cc \\",
            "  --title 'maf-sandbox-acas 0.13.0' --notes-file notes.md &&",
            'gh pr edit 624 --remove-label "autorelease: pending" \\',
            '  --add-label "autorelease: tagged" &&',
            "gh workflow run publish-packages.yml --ref maf-sandbox-acas-v0.13.0 \\",
            "  -f package=maf-sandbox-acas -f target=pypi",
        ]

    def test_the_notes_file_the_create_reads_is_the_one_the_block_writes(self):
        """The block writes the file it then reads, and writes it first."""
        shell = self._shell(report.plan(_document())["body"])
        assert "> notes.md" in shell
        assert shell.index("> notes.md") < shell.index("--notes-file notes.md")

    def test_a_missing_merge_commit_renders_a_bare_word(self):
        """`<merge commit>` is two redirections, so the command ran instead of failing."""
        without = {k: v for k, v in _ACAS.items() if k != "mergeCommit"}
        shell = self._shell(report.body([without], "", released_tags=[], releases=_RELEASES))
        assert "MERGE_COMMIT_SHA" in shell
        assert "<" not in shell

    def test_the_one_argument_holding_a_space_is_single_quoted(self):
        """Double quotes would still expand `$(...)`, so the release title takes single ones."""
        shell = self._shell(report.plan(_document())["body"])
        assert "--title 'maf-sandbox-acas 0.13.0'" in shell


class TestAReleaseThatAlreadyExistsIsNotCreatedAgain:
    """release-please creates the Release, then flips the label. A failure in between leaves a
    pending pull request whose Release is already there, and `gh release create` refuses it."""

    @pytest.fixture
    def text(self) -> str:
        return report.plan(_document(released_tags=["maf-sandbox-acas-v0.13.0"]))["body"]

    def test_it_does_not_tell_anyone_to_create_the_release(self, text: str):
        assert "gh release create" not in text

    def test_it_does_not_claim_to_know_which_call_was_refused(self, text: str):
        """release-please comments on the pull request before it touches the label.

        A refusal there leaves exactly this state too, so naming the label sends the
        investigation to the wrong call. The run log is what actually knows.
        """
        assert "post-release bookkeeping" in text
        assert "run log names the call" in text

    def test_it_names_the_merge_commit_to_check_the_tag_against(self, text: str):
        """An existing tag pointing somewhere else is a different problem, and a worse one."""
        assert _ACAS["mergeCommit"]["oid"] in text

    def test_the_two_remaining_steps_are_still_there(self, text: str):
        assert 'gh pr edit 624 --remove-label "autorelease: pending"' in text
        assert "gh workflow run publish-packages.yml --ref maf-sandbox-acas-v0.13.0" in text

    def test_the_table_says_the_tag_exists(self, text: str):
        assert "`maf-sandbox-acas-v0.13.0` — already created" in text

    def test_another_package_s_release_does_not_count_as_this_one_s(self):
        """Membership, not a prefix: `maf-sandbox-v0.13.0` is a different package's tag."""
        text = report.plan(_document(released_tags=["maf-sandbox-v0.13.0"]))["body"]
        assert "gh release create maf-sandbox-acas-v0.13.0" in text


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

    def test_the_checkout_is_unconditional(self):
        """The step reads `scripts/` off disk, and it runs on runs that release nothing."""
        checkout = _step_named("Check out")
        assert "always()" in str(checkout.get("if", ""))
        assert "actions/checkout@" in checkout["uses"]

    def test_the_checkout_comes_after_release_please(self):
        """In front of it, a failed checkout would stop a release being cut at all.

        release-please and the publish dispatch work over the API and want no workspace, so
        nothing is gained by checking out first and a whole release cycle is risked.
        """
        assert _step_index("Run release-please") < _step_index("Check out")
        assert _step_index("Publish each released package") < _step_index("Check out")

    def test_the_checkout_comes_before_everything_that_reads_it(self):
        for step in ["Propose the dependents' range", "Name any release this run left stuck"]:
            assert _step_index("Check out") < _step_index(step)

    def test_only_one_step_checks_out(self):
        """Two checkouts of the same ref is the shape left behind by moving the first one."""
        checkouts = [step for step in _STEPS if "actions/checkout@" in str(step.get("uses", ""))]
        assert len(checkouts) == 1

    @pytest.mark.parametrize(
        "link",
        [
            'released="$(gh api --paginate "repos/{owner}/{repo}/releases?per_page=100"',
            '--argjson released "$released"',
            "released_tags: $released",
        ],
    )
    def test_it_gathers_which_releases_already_exist(self, link: str):
        """Three links, and each is a separate way for the answer to stop arriving.

        Without the tag list every recovery renders `gh release create`, including the ones
        where the Release is already there and that command is refused. The middle link is the
        one is easy to leave out: the gather, its `--argjson` binding, and the report field
        are three separate links, and the value reaches the report only if all three are there.
        """
        assert link in _step_named("Name any release this run left stuck")["run"]

    def test_the_tracker_it_writes_to_has_to_be_one_a_bot_wrote(self):
        """The marker is in a public script, so anyone with an account can open an issue with it.

        On the marker alone that issue is what gets edited and closed, and the tracker this
        step exists to raise is never opened — the alarm is suppressed by a stranger.
        """
        run = _step_named("Name any release this run left stuck")["run"]
        assert 'select(.user.login == "github-actions[bot]")' in run
        assert "select(.pull_request == null)" in run

    def test_the_tracker_lookup_is_not_a_bounded_page(self):
        """A tracker sinks down a listing as issues are opened while the train stays stuck.

        A page of N stops returning it, after which every run opens a duplicate and none of
        them can close the first.
        """
        run = _step_named("Name any release this run left stuck")["run"]
        assert '--paginate "repos/{owner}/{repo}/issues?state=open&per_page=100"' in run
        assert "gh issue list" not in run

    def test_the_pending_lookup_is_not_a_bounded_page_either(self):
        """`gh pr list` answered newest-first, so its cap dropped the oldest — and the oldest
        is the one release-please stops at, so the report would have named the wrong one."""
        run = _step_named("Name any release this run left stuck")["run"]
        assert "$(gh pr list" not in run
        assert "gh api graphql --paginate" in run
        assert 'labels:["autorelease: pending"]' in run

    def test_it_passes_the_packages_this_repository_releases(self):
        """Without them a title naming any package at all renders a whole recovery."""
        run = _step_named("Name any release this run left stuck")["run"]
        # The gather, its binding, and the report field: three links, and the value reaches
        # the report only if all three are there.
        assert ".release-please-manifest.json" in run
        assert '--argjson releases "$releases"' in run
        assert "releases: $releases" in run

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
