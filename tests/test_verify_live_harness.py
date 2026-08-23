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

    @staticmethod
    def _sample_jobs() -> dict[str, list]:
        """Every job that runs a sample, by name, with its steps."""
        found = {}
        for job, definition in _WORKFLOW.get("jobs", {}).items():
            steps = [step for step in definition.get("steps", []) if isinstance(step, dict)]
            if any("uv run --no-project samples/" in str(s.get("run", "")) for s in steps):
                found[job] = steps
        return found

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
                i
                for i, s in enumerate(steps)
                if "uv run --no-project samples/" in str(s.get("run", ""))
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
