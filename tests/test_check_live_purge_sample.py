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

  wrote a file through the first acquire, read it through the second:
    'still here'
  containers running for this thread: 1

== 2. Between turns: it survives, and that is a decision ==

  turn ended without disposing -> containers still running: 1

== 3. End of turn: `router.scope` disposes however the block ends ==

  inside the turn -> containers running: 1
  block ended -> router reports 1 disposed
  and docker agrees -> containers running: 0

== 4. Thread delete: the backstop ==

  a thread already purged per turn -> purger found 0

  a thread abandoned mid-turn  -> containers running: 1
  user deletes the conversation -> purger found 1
  and docker agrees            -> containers running: 0

Completed 4 of 4 acts. Purger found 0 on a purged thread and 1 on an abandoned one. \
Containers left behind: 0.
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

    def test_a_second_container_is_caught_even_though_the_file_came_back(self):
        # Two acquires that each created a container would still serve the file back from
        # whichever one was written to, so the count is what separates reuse from duplication.
        reasons = check.assess(
            _HEALTHY.replace(
                "containers running for this thread: 1", "containers running for this thread: 2"
            )
        )
        assert any("expected exactly 1" in r for r in reasons), reasons


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
                "and docker agrees -> containers running: 0",
                "and docker agrees -> containers running: 1",
            )
        )
        assert any("still running after the scope block" in r for r in reasons), reasons


class TestTheDeletePath:
    def test_the_purger_finding_nothing_on_the_abandoned_thread_is_caught(self):
        """The one line proving the delete path does something nothing else would.

        A purger wired to nothing reports 0 everywhere, and the tidy thread's 0 is expected —
        so without this the whole act would pass with the hook disconnected.
        """
        reasons = check.assess(
            _HEALTHY.replace(
                "deletes the conversation -> purger found 1",
                "deletes the conversation -> purger found 0",
            )
        )
        assert any("purger wired to nothing also reports 0" in r for r in reasons), reasons

    def test_an_abandoned_thread_with_nothing_running_is_caught(self):
        # If nothing was there, the purger reclaiming it proves nothing either way.
        reasons = check.assess(
            _HEALTHY.replace(
                "abandoned mid-turn  -> containers running: 1",
                "abandoned mid-turn  -> containers running: 0",
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
