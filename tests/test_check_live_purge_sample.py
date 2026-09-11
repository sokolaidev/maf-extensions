"""The match logic behind `scripts/check_live_purge_sample.py`, tested on every PR.

`_HEALTHY` is a real run's output, trimmed — checked against one rather than written from
memory, since a fixture that has drifted makes every assertion below pass against a fiction.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_live_purge_sample.py"
_spec = importlib.util.spec_from_file_location("check_live_purge_sample", _SCRIPT)
assert _spec and _spec.loader
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

_HEALTHY = """\
== 1. Within a turn: get-or-create is the point ==
    'still here'
  containers for this thread: 1
== 2. Between turns: it survives, and that is a decision ==
  turn ended without disposing -> containers still there: 1
== 3. End of turn: `router.scope` disposes however the block ends ==
  inside the turn -> containers: 1
  block ended -> router reports 1 disposed
  and docker agrees -> containers: 0
== 4. Thread delete: the backstop ==
  a thread already purged per turn -> purger found 0
  a thread never scoped per turn -> containers: 1
  user deletes the conversation  -> purger found 1
  and docker agrees, after purge -> containers: 0
== 5. When the cleanup cannot run: the framework acts, then tells you ==
  [measured] Cleanup rung for this call: reclaim
  containers after the escalation: 0
  what a collector received for call a6ea1a6340c54c539a9e1f2b1cb657d3:
    sandbox.call                 maf_sandbox.call.unclean = 0
    sandbox.dispose              2 record(s), maf_sandbox.disposal.outcome = gone
    app.sandbox.reclaim_failure  app.reclaim.disposal = disposed
  [measured] Disposal records for this call: 2
  [measured] Recorded disposal: disposed
  [measured] Recorded path: a6ea1a6340c54c539a9e1f2b1cb657d3
  [measured] Recorded reason: the removal call failed: OSError: could not reclaim /maf-sandbox/work/a6ea1a6340c54c539a9e1f2b1c
== 6. `FailedReclaimPolicy.KEEP`: the same failure, kept on purpose ==
  containers kept after the failure: 1
  what a collector received for call baf565c1efd9445bbc9f3cf53052fa4c:
    sandbox.call                 maf_sandbox.call.unclean = 0
    sandbox.dispose              1 record(s), maf_sandbox.disposal.outcome = gone
    app.sandbox.reclaim_failure  app.reclaim.disposal = kept
  [measured] Disposal records for this call: 1
  [measured] Recorded disposal: kept
  [measured] Recorded path: baf565c1efd9445bbc9f3cf53052fa4c
  [measured] Recorded reason: the removal call failed: OSError: could not reclaim /maf-sandbox/work/baf565c1efd9445bbc9f3cf530
== 7. `disposal='failed'`: the remedy that could not be proved ==
  what a collector received for call 54c045740fe747dab56de45b5dbd65d9:
    sandbox.call                 maf_sandbox.call.unclean = 0
    sandbox.dispose              2 record(s), maf_sandbox.disposal.outcome = gone, may_remain
    app.sandbox.reclaim_failure  app.reclaim.disposal = failed
  [measured] Disposal records for this call: 2
  [measured] Recorded disposal: failed
  [measured] Recorded path: 54c045740fe747dab56de45b5dbd65d9
  [measured] Recorded reason: the removal call failed: TimeoutError: ; timeout: docker: the delete did not finish within 0.001
  [measured] The next acquire on that key: SandboxUnclean
  containers left by the unproved disposal: 0
== 8. A handler that raises is contained ==
  [measured] The raise reached the caller: unchanged
  [measured] Records made by the handler that raised: 1
  containers after the raising handler: 0
Completed 8 of 8 acts. Purger found 0 on a purged thread and 1 on an unscoped one. Kept after a failed reclaim: 1. The key after an unprovable disposal: SandboxUnclean. The answer under a raising handler: unchanged. Reclaim failures recorded: 4. Containers left behind: 0.
"""

#: Every line acts 5 and 6 print only because a cleanup failed. Removing them is what a run
#: whose forced failure stopped being forced looks like: four healthy acts, two quiet ones, and
#: a handler nobody called — the silent pass this check exists to refuse (#760).
_ONLY_WHEN_IT_FAILED = (
    "  [measured] Recorded disposal: ",
    "  [measured] Recorded reason: ",
    "  [measured] Disposal records for this call: ",
    "  what a collector received for call ",
    "  [measured] Recorded path: ",
    "  [measured] Records made by the handler that raised: ",
    "    sandbox.call ",
    "    sandbox.dispose ",
    "    app.sandbox.reclaim_failure ",
)


#: The first act's call id, for the tamper tests that have to name one.
_FIRST_CALL = "a6ea1a6340c54c539a9e1f2b1cb657d3"


def _without_the_handlers_records(output: str) -> str:
    """``output`` with every line only a fired handler produces removed."""
    return "\n".join(
        line for line in output.splitlines() if not line.startswith(_ONLY_WHEN_IT_FAILED)
    )


class TestHealthyRun:
    def test_a_real_run_passes(self):
        assert check.assess(_HEALTHY) == []


class TestReuseWithinATurn:
    def test_state_not_surviving_the_second_acquire_is_caught(self):
        """The claim is the same *sandbox*, tested the way a workload feels it.

        `is` would have tested the same Python object, which the protocol does not promise and
        which the docker backend does not provide — it hands back a fresh handle over the same
        container. Asserting identity would fail against a correct backend.
        """
        reasons = check.assess(_HEALTHY.replace("'still here'", "''"))
        assert any("second acquire reached a different sandbox" in r for r in reasons), reasons

    def test_a_container_left_beside_the_reused_one_is_caught(self):
        # Not the same thing as the file round-trip. If the second acquire had made its own
        # container the `cat` would have failed, so that check already covers "reached a
        # different sandbox"; this covers a container created and then orphaned alongside.
        reasons = check.assess(
            _HEALTHY.replace("containers for this thread: 1", "containers for this thread: 2")
        )
        assert any("orphaned beside it" in r for r in reasons), reasons


class TestBetweenTurns:
    def test_a_sandbox_that_did_not_survive_the_turn_is_caught(self):
        """Act 2's premise. Without it there is nothing for the rest of the sample to decide."""
        tampered = _HEALTHY.replace("containers still there: 1", "containers still there: 0")
        assert tampered != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(tampered)
        assert any("does not outlive its turn" in r for r in reasons), reasons


class TestEndOfTurnDisposal:
    def test_a_scope_block_that_reclaimed_nothing_is_caught(self):
        reasons = check.assess(
            _HEALTHY.replace("router reports 1 disposed", "router reports 0 disposed")
        )
        assert any("expected exactly 1" in r for r in reasons), reasons

    def test_a_container_surviving_the_block_is_caught(self):
        # The router's count is its own claim; this is docker's answer, and they can disagree.
        reasons = check.assess(
            _HEALTHY.replace(
                "and docker agrees -> containers: 0",
                "and docker agrees -> containers: 1",
            )
        )
        assert any("still running after the scope block" in r for r in reasons), reasons


class TestTheDeletePath:
    def test_the_purger_finding_nothing_on_the_never_scoped_thread_is_caught(self):
        """The one line proving the delete path does something nothing else would.

        A purger wired to nothing reports 0 everywhere, and the tidy thread's 0 is expected —
        so without this the whole act would pass with the hook disconnected.
        """
        tampered = _HEALTHY.replace("purger found 1", "purger found 0")
        assert tampered != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(tampered)
        assert any("purger wired to nothing also reports 0" in r for r in reasons), reasons

    def test_a_never_scoped_thread_with_nothing_running_is_caught(self):
        # If nothing was there, the purger reclaiming it proves nothing either way.
        reasons = check.assess(
            _HEALTHY.replace(
                "never scoped per turn -> containers: 1",
                "never scoped per turn -> containers: 0",
            )
        )
        assert any("proves nothing" in r for r in reasons), reasons

    def test_the_tidy_thread_finding_something_is_caught(self):
        reasons = check.assess(
            _HEALTHY.replace(
                "already purged per turn -> purger found 0",
                "already purged per turn -> purger found 1",
            )
        )
        assert any("already purged per turn" in r for r in reasons), reasons

    def test_a_purge_that_reported_but_reclaimed_nothing_is_caught(self):
        """The hole the footer cannot cover.

        `main` sweeps every thread in a `finally` before computing the footer, so a purger that
        reported 1 while removing nothing would be cleaned up by that sweep and `Containers left
        behind` would still read 0. This is the only line that sees the machine in between.
        """
        tampered = _HEALTHY.replace("after purge -> containers: 0", "after purge -> containers: 1")
        assert tampered != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(tampered)
        assert any("reported reclaiming and did not" in r for r in reasons), reasons

    def test_act_threes_count_is_not_read_as_act_fours(self):
        # The two `and docker agrees` lines once differed only in whitespace, so `search` found
        # act 3's for both and act 4's went unchecked. Their wording is distinct now.
        assert check._SCOPE_REMAINING.search(_HEALTHY).group(1) == "0"
        assert check._UNSCOPED_AFTER.search(_HEALTHY).group(1) == "0"
        act_four_only = _HEALTHY.replace(
            "after purge -> containers: 0", "after purge -> containers: 3"
        )
        assert check._SCOPE_REMAINING.search(act_four_only).group(1) == "0", (
            "act 4's line must not be what act 3's pattern reads"
        )
        assert check._UNSCOPED_AFTER.search(act_four_only).group(1) == "3"


class TestTheFooter:
    def test_a_leaked_container_fails(self):
        """A sample about reclaiming sandboxes may not leave one running."""
        reasons = check.assess(
            _HEALTHY.replace("Containers left behind: 0.", "Containers left behind: 1.")
        )
        assert any("left behind" in r for r in reasons), reasons

    def test_a_footer_disagreeing_with_the_acts_is_caught(self):
        reasons = check.assess(
            _HEALTHY.replace(
                "Purger found 0 on a purged thread and 1", "Purger found 1 on a purged thread and 0"
            )
        )
        assert any("summary and the run disagree" in r for r in reasons), reasons

    def test_a_partial_run_is_caught(self):
        reasons = check.assess(_HEALTHY.replace("Completed 8 of 8", "Completed 2 of 8"))
        assert any("2 of 8 acts completed" in r for r in reasons), reasons

    def test_a_truncated_run_has_no_footer(self):
        cut = _HEALTHY[: _HEALTHY.index("Completed 8 of 8")]
        assert any("did not run to completion" in r for r in check.assess(cut))

    def test_a_footer_reporting_no_reclaim_failures_is_caught(self):
        """The one number here that fails on nought, and the reason this check was extended.

        Every other footer value is a leak count that passes at zero. This one passes at two:
        a handler nothing ever calls reports nought and a check reading it agrees (#760).
        """
        reasons = check.assess(
            _HEALTHY.replace("Reclaim failures recorded: 4", "Reclaim failures recorded: 0")
        )
        assert any("0 reclaim failure(s) recorded" in r for r in reasons), reasons

    def test_a_footer_disagreeing_about_what_was_kept_is_caught(self):
        reasons = check.assess(
            _HEALTHY.replace("Kept after a failed reclaim: 1", "Kept after a failed reclaim: 0")
        )
        assert any("kept after a failed reclaim, expected 1" in r for r in reasons), reasons


class TestTheCleanupThatFailed:
    """Acts 5 to 8: the only place in the suite where `on_reclaim_failure` is called at all."""

    def test_a_rung_below_reclaim_means_nothing_was_exercised(self):
        """At any other rung the call's directory is never removed on its own.

        The container counts would still read 0 and 1 for unrelated reasons, so without this the
        acts could pass having demonstrated nothing.
        """
        reasons = check.assess(
            _HEALTHY.replace(
                "Cleanup rung for this call: reclaim", "Cleanup rung for this call: dispose"
            )
        )
        assert any("expected 'reclaim'" in r for r in reasons), reasons

    def test_an_escalation_that_left_the_container_running_is_caught(self):
        reasons = check.assess(
            _HEALTHY.replace(
                "containers after the escalation: 0", "containers after the escalation: 1"
            )
        )
        assert any("after a reclaim that failed, expected 0" in r for r in reasons), reasons

    def test_keep_behaving_like_dispose_is_caught(self):
        """The two policies collapsing is the failure act 6 exists to rule out."""
        reasons = check.assess(
            _HEALTHY.replace(
                "containers kept after the failure: 1", "containers kept after the failure: 0"
            )
        )
        assert any("expected exactly 1" in r for r in reasons), reasons

    def test_a_handler_that_never_fired_is_caught(self):
        """The silent pass: every act healthy, every leak count nought, and no record made."""
        reasons = check.assess(_without_the_handlers_records(_HEALTHY))
        assert any("expected ['disposed', 'kept', 'failed']" in r for r in reasons), reasons
        assert any("0 recorded reason(s)" in r for r in reasons), reasons

    def test_a_reason_that_was_not_recorded_is_caught(self):
        """`(absent)` is what the sample prints for an attribute that stopped being written."""
        reasons = check.assess(
            _HEALTHY.replace(
                "Recorded reason: the removal call failed: OSError: could not reclaim /maf-sandbox/work/a6ea1a6340c54c539a9e1f2b1c",
                "Recorded reason: (absent)",
            )
        )
        assert any("one of the three facts" in r for r in reasons), reasons

    def test_unclean_counting_the_failed_removal_is_caught(self):
        """Not a defect if it changes — but the sample's argument would no longer hold.

        `maf_sandbox.call.unclean` counts processes a transport could not prove it stopped. The
        acts argue that a host needs its own record because that attribute does not see a
        removal. If core ever folds the two together, this fails and the prose gets rewritten.
        """
        reasons = check.assess(
            _HEALTHY.replace("maf_sandbox.call.unclean = 0", "maf_sandbox.call.unclean = 1", 1)
        )
        assert any("expected ['0', '0', '0']" in r for r in reasons), reasons

    def test_one_act_reading_another_s_records_is_caught(self):
        """What `for_call` selecting on the call id prevents, asserted from the output."""
        reasons = check.assess(
            _HEALTHY.replace("baf565c1efd9445bbc9f3cf53052fa4c", "a6ea1a6340c54c539a9e1f2b1cb657d3")
        )
        assert any("so one of them read another's" in r for r in reasons), reasons

    def test_the_disposal_record_counts_are_read_in_order(self):
        """Two for act 5, one for act 6, two for act 7 — and changing one is caught."""
        reasons = check.assess(
            _HEALTHY.replace(
                "Disposal records for this call: 2", "Disposal records for this call: 1", 1
            )
        )
        assert any("expected ['2', '1', '2']" in r for r in reasons), reasons

    def test_a_recorded_path_that_names_another_call_is_caught(self):
        """The only assertion on `app.reclaim.path`, and the reason it is not a tautology.

        The path comes from the host's own record and the call id from the package's, so the two
        agreeing is two independently produced records agreeing. Drop `PATH` from the handler and
        this is the line that goes red.
        """
        reasons = check.assess(
            _HEALTHY.replace(f"Recorded path: {_FIRST_CALL}", "Recorded path: " + "0" * 32)
        )
        assert any("has to name the call directory" in r for r in reasons), reasons

    def test_a_key_served_after_an_unprovable_disposal_is_caught(self):
        """Act 7's whole claim: what cannot be proved clean is refused, not served."""
        reasons = check.assess(
            _HEALTHY.replace(
                "The next acquire on that key: SandboxUnclean",
                "The next acquire on that key: served",
            )
        )
        assert any("expected 'SandboxUnclean'" in r for r in reasons), reasons

    def test_a_raise_that_replaced_the_answer_is_caught(self):
        """A host cannot turn a cleanup problem into a failed turn by writing a bad callback."""
        reasons = check.assess(
            _HEALTHY.replace(
                "The raise reached the caller: unchanged",
                "The raise reached the caller: replaced",
            )
        )
        assert any("expected 'unchanged'" in r for r in reasons), reasons

    def test_a_handler_that_raised_before_recording_is_caught(self):
        """The #721 defect class, in the direction that looks healthy.

        Reversing the handler's two lines loses the record and changes no count anywhere else,
        so this line is the only thing standing between that order and a green run.
        """
        reasons = check.assess(
            _HEALTHY.replace(
                "Records made by the handler that raised: 1",
                "Records made by the handler that raised: 0",
            )
        )
        assert any("recorded 0, expected 1" in r for r in reasons), reasons


class TestEmptyOutput:
    def test_nothing_passes_vacuously(self):
        assert check.assess("") != []


class TestALineReportedTwiceIsRefused:
    """The failure sample 10's check shipped with: a second line, and the first believed."""

    def test_every_line_is_reported_once_in_a_healthy_run(self):
        assert check._assess_each_line_appears_once(_HEALTHY) == []

    def test_a_second_line_of_the_same_shape_is_named(self):
        doubled = _HEALTHY.replace(
            "  block ended -> router reports 1 disposed",
            "  block ended -> router reports 1 disposed\n  block ended -> router reports 0 disposed",
        )
        reasons = check.assess(doubled)
        assert any("what the scope disposed is reported on 2 lines" in r for r in reasons), reasons

    def test_the_first_of_two_is_not_taken_as_the_answer(self):
        """Ordered so the first line reads healthy: a checker taking it would pass."""
        doubled = _HEALTHY.replace(
            "  and docker agrees -> containers: 0",
            "  and docker agrees -> containers: 0\n  and docker agrees -> containers: 9",
        )
        assert check.assess(doubled) != []
