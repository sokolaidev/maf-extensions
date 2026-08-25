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
== 5. Cleanup that did not work: the moment nobody chose ==
  default posture       -> disposal=disposed, and the backend was asked to dispose it 1 time(s)
  keep_unclean=True     -> disposal=kept, and the backend was asked to dispose it 0 time(s)
  and the next acquire read the call's file back: 'left behind'
  a disposal that fails -> disposal=failed, and the next call was refused:
    'Error: the sandbox for this conversation is closed: a previous call left it unclean — data that could not be removed, or a program that may still be running — and it could not be disposed. Nothing runs in it until it is.'
Completed 5 of 5 acts. Purger found 0 on a purged thread and 1 on an unscoped one. The three unclean postures reported disposed, kept, failed. Containers left behind: 0.
"""


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


class TestTheCallThatCouldNotBeCleaned:
    """Act 5: one failure under three postures, which the check has to tell apart.

    All three report a `ReclaimFailure`, so the disposal word alone does not say what the
    framework actually did about the sandbox — the count of disposals the backend was asked for
    is what does, and a posture quietly behaving like another is the failure worth catching.
    """

    def test_a_default_posture_that_disposed_nothing_is_caught(self):
        tampered = _HEALTHY.replace(
            "disposal=disposed, and the backend was asked to dispose it 1",
            "disposal=disposed, and the backend was asked to dispose it 0",
        )
        assert tampered != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(tampered)
        assert any("expected disposed after exactly 1" in r for r in reasons), reasons

    def test_an_opt_down_that_disposed_anyway_is_caught(self):
        tampered = _HEALTHY.replace(
            "disposal=kept, and the backend was asked to dispose it 0",
            "disposal=kept, and the backend was asked to dispose it 1",
        )
        assert tampered != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(tampered)
        assert any("opt-down that disposes anyway" in r for r in reasons), reasons

    def test_a_kept_sandbox_that_gave_nothing_back_is_caught(self):
        """The retention shown as data. A read that comes back empty proves the opposite."""
        tampered = _HEALTHY.replace(
            "read the call's file back: 'left behind'", "read the call's file back: ''"
        )
        assert tampered != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(tampered)
        assert any("did not reach the file" in r for r in reasons), reasons

    def test_a_missing_read_back_is_caught(self):
        cut = _HEALTHY.replace(
            "  and the next acquire read the call's file back: 'left behind'\n", ""
        )
        assert cut != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(cut)
        assert any("a word rather than a file" in r for r in reasons), reasons

    def test_a_disposal_that_failed_and_said_otherwise_is_caught(self):
        tampered = _HEALTHY.replace(
            "a disposal that fails -> disposal=failed",
            "a disposal that fails -> disposal=disposed",
        )
        assert tampered != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(tampered)
        assert any("expected failed" in r for r in reasons), reasons

    def test_a_next_call_that_was_not_refused_is_caught(self):
        """The consequence a caller sees with no callback wired at all, so the one that must
        not be reported as healthy: a router still serving the key it could not clean."""
        served = (
            "\n".join(line for line in _HEALTHY.splitlines() if check._CLOSED not in line) + "\n"
        )
        assert served != _HEALTHY, "the fixture moved"
        reasons = check.assess(served)
        assert any("goes on serving a key it could not clean" in r for r in reasons), reasons


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
        reasons = check.assess(_HEALTHY.replace("Completed 5 of 5", "Completed 2 of 5"))
        assert any("2 of 5 acts completed" in r for r in reasons), reasons

    def test_a_truncated_run_has_no_footer(self):
        cut = _HEALTHY[: _HEALTHY.index("Completed 5 of 5")]
        assert any("did not run to completion" in r for r in check.assess(cut))

    def test_a_footer_disagreeing_with_act_five_is_caught(self):
        tampered = _HEALTHY.replace(
            "postures reported disposed, kept, failed", "postures reported disposed, kept, kept"
        )
        assert tampered != _HEALTHY, "the substitution matched nothing — the fixture moved"
        reasons = check.assess(tampered)
        assert any("act 5 reported disposed, kept and failed" in r for r in reasons), reasons


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
