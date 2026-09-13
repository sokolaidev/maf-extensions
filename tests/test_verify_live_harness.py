"""The live checks are read from the default branch, not from the ref under test.

`verify-live.yml` installs the *published* wheels and runs the samples from the ref that
triggered it — a release tag, for the call `publish-packages.yml` makes after an upload. The
samples belong there: they are the code that shipped. The `scripts/check_live_*.py` assertions
do not, because they are the test rather than its subject, and a test frozen at the tag cannot
be fixed for a release already cut (#318).

That is wiring, so nothing else fails when it comes undone — a new sample job written from a
copy of an existing one is the likely way, and it would go on passing while quietly asserting
with whatever the tag carried. These pin the shape instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_LIVE = REPO_ROOT / ".github" / "workflows" / "verify-live.yml"
_TEXT = VERIFY_LIVE.read_text("utf-8")
_WORKFLOW = yaml.safe_load(_TEXT)

#: Any invocation of a live check, whatever it is prefixed with — the prefix is the assertion.
_CHECK_CALL = re.compile(r"python3\s+(?P<prefix>\S*?)scripts/(?P<script>check_live_\w+\.py)")

#: The second checkout, the one that fetches the checks. Keyed on the path rather than the step
#: name, since a name is prose and this is the thing that decides where they come from.
_HARNESS_CHECKOUT = re.compile(r"^\s*path:\s*\.harness\s*$", re.MULTILINE)

#: A step that resolves a sample and runs it. `$source_args` sits between the flag and the path
#: when `scripts/sample_source_args.py` has something to inject, so this matches across it: a
#: detector that stopped recognising those jobs would make every assertion over them vacuous.
_RESOLVES_SAMPLE = re.compile(r"uv run --no-project (?:\$source_args )?samples/")

#: The sample directory a step asks `sample_source_args.py` about, and the one it then runs.
_ASKS_FOR = re.compile(r"sample_source_args\.py (samples/[0-9A-Za-z_]+)")
_RUNS = re.compile(r"uv run --no-project (?:\$source_args )?(samples/[0-9A-Za-z_]+)/agent\.py")


def sample_jobs() -> dict[str, list]:
    """Every job that runs a sample directly, by name, with its steps."""
    found = {}
    for job, definition in _WORKFLOW.get("jobs", {}).items():
        steps = [step for step in definition.get("steps", []) if isinstance(step, dict)]
        if any(_RESOLVES_SAMPLE.search(str(s.get("run", ""))) for s in steps):
            found[job] = steps
    return found


class TestEveryLiveCheckComesFromTheHarness:
    def test_the_workflow_still_runs_live_checks(self):
        # Without this the two tests below pass vacuously on a file that stopped invoking any.
        assert len(_CHECK_CALL.findall(_TEXT)) >= 7, "verify-live.yml invokes no live checks"

    def test_no_check_is_run_from_the_ref_under_test(self):
        """A bare `scripts/…` reads the checked-out ref, which for a release call is the tag."""
        bare = [
            m.group("script")
            for m in _CHECK_CALL.finditer(_TEXT)
            if m.group("prefix") != '"$HARNESS"/'
        ]
        assert not bare, f"invoked from the ref under test rather than $HARNESS: {bare}"

    def test_each_invoking_job_checks_the_harness_out(self):
        # One `path: .harness` per job that runs a check, regardless of how many live checks
        # that job invokes. The harness is a job-scoped dependency: a single checkout feeds every
        # `python3 "$HARNESS"/scripts/check_live_*.py` call in that job, and a tag run fails if
        # the job reads the ref under test instead.
        jobs = _WORKFLOW.get("jobs", {})
        live_jobs = []
        for job, definition in jobs.items():
            steps = definition.get("steps", [])
            if any(
                'python3 "$HARNESS"/scripts/check_live_' in str(step.get("run", ""))
                for step in steps
            ):
                live_jobs.append(job)
                assert any(
                    step.get("with", {}).get("path") == ".harness"
                    for step in steps
                    if isinstance(step, dict)
                ), f"{job} invokes a live check without checking out the harness"
        assert live_jobs, "verify-live.yml invokes no live checks"


class TestTheFallbackKeepsABranchDispatchHonest:
    def test_a_non_tag_ref_runs_its_own_checks(self):
        """Dispatching from a branch is how a change to a check gets tried out.

        If `HARNESS` pointed at the default branch unconditionally, that dispatch would run
        `main`'s copy and report on code the run never executed.
        """
        assert re.search(
            r"HARNESS:\s*\$\{\{\s*startsWith\(github\.ref,\s*'refs/tags/'\)\s*&&\s*"
            r"'\.harness'\s*\|\|\s*'\.'\s*\}\}",
            _TEXT,
        ), "HARNESS no longer falls back to the working tree for a non-tag ref"

    def test_the_harness_checkout_is_conditional_on_the_same_predicate(self):
        # The `if:` and `HARNESS` have to agree, or a run either clones what it will not read or
        # reads what it did not clone.
        conditions = re.findall(
            r"^\s*if:\s*startsWith\(github\.ref,\s*'refs/tags/'\)\s*$", _TEXT, re.MULTILINE
        )
        assert len(conditions) == len(_HARNESS_CHECKOUT.findall(_TEXT))


class TestASampleWaitsForItsOwnEdgeBeforeItResolves:
    """A live run that resolved the previous release measures the wrong thing, quietly (#595).

    `wait-for-propagation` confirms the upload on a different runner, and PyPI's index is
    eventually consistent between them — so the wait has to happen where the resolving happens.
    """

    _sample_jobs = staticmethod(sample_jobs)

    def test_the_workflow_still_runs_samples(self):
        # Without this the tests below pass vacuously on a file that stopped running any.
        assert len(self._sample_jobs()) >= 7

    def test_every_sample_job_waits_first(self):
        for job, steps in self._sample_jobs().items():
            assert any("await_live_version.py" in str(step.get("run", "")) for step in steps), (
                f"{job} resolves a sample without waiting for this runner's edge"
            )

    def test_the_wait_runs_before_the_sample_rather_than_after_it(self):
        """Ordered after it, the sample has already resolved and the wait proves nothing."""
        for job, steps in self._sample_jobs().items():
            waits = next(
                i for i, s in enumerate(steps) if "await_live_version.py" in str(s.get("run", ""))
            )
            resolves = next(
                i for i, s in enumerate(steps) if _RESOLVES_SAMPLE.search(str(s.get("run", "")))
            )
            assert waits < resolves, f"{job} waits for the edge after resolving against it"

    def test_the_wait_comes_from_the_harness(self):
        """Same reason every check does: a tag's copy cannot be fixed for a release already cut."""
        for job, steps in self._sample_jobs().items():
            for step in steps:
                run = str(step.get("run", ""))
                if "await_live_version.py" in run:
                    assert '"$HARNESS"/scripts/await_live_version.py' in run, job

    def test_a_run_with_no_published_version_does_not_wait_for_one(self):
        """A branch dispatch names no version, and there is nothing on PyPI to wait for."""
        for job, steps in self._sample_jobs().items():
            for step in steps:
                if "await_live_version.py" in str(step.get("run", "")):
                    assert "inputs.version != ''" in str(step.get("if", "")), job


class TestTheSourceUnderTestIsWhatTheInputSays:
    """Which libraries a sample runs against is a silent property of the job.

    Both directions fail quietly. A `branch` run that forgot to inject resolves the index and
    reports the branch green; a `published` run that injected anything stops measuring the thing
    a release verification exists to measure. Nothing in the run log says which happened, so the
    wiring is pinned here instead.
    """

    def test_every_sample_job_asks_which_source_to_run(self):
        for job, steps in sample_jobs().items():
            assert any("sample_source_args.py" in str(s.get("run", "")) for s in steps), (
                f"{job} runs a sample without asking where its libraries come from"
            )

    def test_the_question_comes_from_the_harness(self):
        """Same reason every check does: a tag's copy cannot be fixed for a release already cut."""
        for job, steps in sample_jobs().items():
            for step in steps:
                run = str(step.get("run", ""))
                if "sample_source_args.py" in run:
                    assert '"$HARNESS"/scripts/sample_source_args.py' in run, job

    def test_each_job_asks_about_the_sample_it_runs(self):
        """A step copied from another job would otherwise inject that one's packages."""
        for job, steps in sample_jobs().items():
            for step in steps:
                run = str(step.get("run", ""))
                asked, ran = _ASKS_FOR.findall(run), _RUNS.findall(run)
                if ran:
                    assert asked == ran, f"{job} asks about {asked} and runs {ran}"

    def test_the_answer_reaches_the_command(self):
        """Computing the arguments and not passing them is the quietest way to lose this."""
        for job, steps in sample_jobs().items():
            for step in steps:
                run = str(step.get("run", ""))
                if _RUNS.search(run):
                    assert "$source_args" in run, f"{job} computes the source and drops it"

    def test_a_branch_run_asserts_no_published_version(self):
        """In-tree versions are the last released ones, so the assertion would pass regardless."""
        gated = [
            step
            for steps in sample_jobs().values()
            for step in steps
            if "check_live_versions.py" in str(step.get("run", ""))
        ]
        assert gated, "no job asserts a resolved version any more"
        for step in gated:
            assert "inputs.source != 'branch'" in str(step.get("if", "")), step.get("name")

    def test_both_entry_points_take_the_input(self):
        for trigger in ("workflow_dispatch", "workflow_call"):
            inputs = _WORKFLOW[True][trigger]["inputs"]
            assert "source" in inputs, trigger
            assert inputs["source"]["default"] == "published", trigger

    def test_the_dispatch_offers_exactly_the_two_modes(self):
        options = _WORKFLOW[True]["workflow_dispatch"]["inputs"]["source"]["options"]
        assert options == ["published", "branch"]

    def test_the_retried_samples_are_injected_too(self):
        """13, 15 and 15-docker run through the retry harness, so the flags cannot be shell-side."""
        harness = (REPO_ROOT / "scripts" / "retry_live_sample.py").read_text("utf-8")
        assert "sample_source_args" in harness
