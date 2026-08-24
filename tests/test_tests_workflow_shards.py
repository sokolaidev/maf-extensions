"""Pin the invariants that let `tests.yml` sit behind two required contexts while its work is
sharded across parallel jobs.

`main`'s ruleset names exactly two contexts, and a context that stops reporting leaves every
open pull request unmergeable against a check nobody can produce. So the shard *names* are free
to change and these two are not: the join job's `name:`, and `docker-e2e`'s. What the join
does — refuse unless every shard succeeded — is pinned here too, because a join that passed on
a failed shard would turn the required check into decoration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = yaml.safe_load(
    (REPO_ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
)
JOBS = WORKFLOW["jobs"]

#: The join job, keyed by the name `main` requires rather than by its job id.
_REQUIRED_CONTEXT = "Python (pytest + ruff + pyright)"
_LIVE_CONTEXT = "Docker backend live tests"


def _job_named(display_name: str) -> dict:
    for job in JOBS.values():
        if job.get("name") == display_name:
            return job
    raise AssertionError(
        f"no job in tests.yml is named {display_name!r}. That name is a required status check "
        "on `main`: a pull request waits for a context that will never report, for ever."
    )


class TestTheRequiredContextsStillReport:
    def test_the_join_job_keeps_the_required_name(self):
        _job_named(_REQUIRED_CONTEXT)

    def test_the_live_job_keeps_the_required_name(self):
        _job_named(_LIVE_CONTEXT)

    def test_the_live_job_is_not_behind_the_join(self):
        """It is a required context in its own right, so joining it would hide its verdict."""
        join = _job_named(_REQUIRED_CONTEXT)
        live_id = next(key for key, job in JOBS.items() if job.get("name") == _LIVE_CONTEXT)
        assert live_id not in join["needs"]


class TestTheJoinRefusesAnythingButSuccess:
    def test_it_waits_for_every_other_job(self):
        """A shard nobody joined is a check whose failure the required context never sees."""
        join_id = next(key for key, job in JOBS.items() if job.get("name") == _REQUIRED_CONTEXT)
        live_id = next(key for key, job in JOBS.items() if job.get("name") == _LIVE_CONTEXT)
        joined = set(JOBS[join_id]["needs"])
        unjoined = set(JOBS) - joined - {join_id, live_id}
        assert not unjoined, (
            f"these jobs are in no required context: {sorted(unjoined)}. Add them to the join's "
            "`needs`, or their failures cannot block a merge."
        )

    def test_it_runs_even_when_a_shard_failed(self):
        """Without `always()` the join is skipped, and a skipped required check never reports."""
        join = _job_named(_REQUIRED_CONTEXT)
        assert "always()" in str(join.get("if", "")), (
            "the join must run on `if: always()`. Skipped, it reports nothing, and a required "
            "check that never reports is a pull request that can never merge."
        )

    @pytest.mark.parametrize("verdict", ["failure", "cancelled", "skipped"])
    def test_no_shard_verdict_but_success_passes(self, verdict: str):
        """The step's own rule, read out of the workflow: anything but `success` exits 1."""
        step = next(
            step
            for step in _job_named(_REQUIRED_CONTEXT)["steps"]
            if "RESULTS" in str(step.get("env", {}))
        )
        body = step["run"]
        assert '!= "success"' in body, (
            f"the join accepts a shard whose result is {verdict!r}: it must compare against "
            '"success" rather than listing the failures it knows about today.'
        )
        assert "exit 1" in body


class TestTheShardsNeverSkipWholesale:
    """A skipped job counts as success to a required check, so the shards skip *steps* instead —
    the reason `tests.yml` carries no `paths-ignore` either (#560)."""

    def test_no_job_carries_a_job_level_if(self):
        offenders = [
            key for key, job in JOBS.items() if job.get("name") != _REQUIRED_CONTEXT and "if" in job
        ]
        assert not offenders, (
            f"{sorted(offenders)} skip wholesale on a condition. Put the condition on the "
            "steps: a skipped job reports success without having run anything."
        )

    def test_every_shard_classifies_before_it_runs(self):
        """Each shard reads the one `changes` verdict rather than re-deriving the rule."""
        for key, job in JOBS.items():
            if key == "changes" or job.get("name") == _REQUIRED_CONTEXT:
                continue
            assert "changes" in job.get("needs", []), f"{key} does not wait for `changes`"
