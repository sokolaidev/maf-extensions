"""Keep the required Python join reporting and refusing any unsuccessful shard."""

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


class TestTheJoinRefusesAnythingButSuccess:
    def test_it_waits_for_every_other_job(self):
        """A shard nobody joined is a check whose failure the required context never sees."""
        join_id = next(key for key, job in JOBS.items() if job.get("name") == _REQUIRED_CONTEXT)
        joined = set(JOBS[join_id]["needs"])
        unjoined = set(JOBS) - joined - {join_id}
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


def test_public_https_relay_requires_explicit_dispatch_opt_in():
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))
    option = triggers["workflow_dispatch"]["inputs"]["hyperlight_https"]
    assert option["type"] == "boolean" and option["default"] is False
    steps = JOBS["hyperlight-linux-worker"]["steps"]
    relay_steps = [step for step in steps if "cloudflared" in step.get("run", "")]
    assert len(relay_steps) == 2
    for step in relay_steps:
        assert step["if"] == "github.event_name == 'workflow_dispatch' && inputs.hyperlight_https"
    assert "sha256sum --check" in relay_steps[0]["run"]
    assert "MAF_HYPERLIGHT_HTTPS_LIVE=1" in relay_steps[1]["run"]
    assert "check_hyperlight_linux.py --live" in relay_steps[1]["run"]
