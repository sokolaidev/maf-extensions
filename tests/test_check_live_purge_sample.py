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
Completed 4 of 4 acts. Purger found 0 on a purged thread and 1 on an unscoped one. Containers left behind: 0.
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
        reasons = check.assess(_HEALTHY.replace("Completed 4 of 4", "Completed 2 of 4"))
        assert any("2 of 4 acts completed" in r for r in reasons), reasons

    def test_a_truncated_run_has_no_footer(self):
        cut = _HEALTHY[: _HEALTHY.index("Completed 4 of 4")]
        assert any("did not run to completion" in r for r in check.assess(cut))


class TestEmptyOutput:
    def test_nothing_passes_vacuously(self):
        assert check.assess("") != []
