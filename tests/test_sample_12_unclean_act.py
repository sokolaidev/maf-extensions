"""Sample 12's fifth act, run for real on every pull request.

Acts 1 to 4 need a Docker engine and only the live job has one. **Act 5 needs nothing**: it
stages a reclaim that refuses on the in-process backend, so the one act whose subject is a
failure is also the one that can be exercised here — which is worth doing, because it is the
act a reader is least able to check by running it against their own deployment.

Two things are pinned. The three postures produce three different outcomes: the framework
disposing what it could not clean, the host opting down and keeping it with the data in it, and
a disposal that did not land leaving the key refused. And the lines the act prints are the lines
`scripts/check_live_purge_sample.py` reads — a fixture in that suite proves the check against a
transcript, and this proves the transcript against the sample.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE = _ROOT / "samples" / "12_purge_lifecycle"


def _load_sample() -> ModuleType:
    """Import sample 12's ``agent.py``, leaving ``sys.path`` and ``sys.modules`` as they were.

    The sample imports ``_scaffold`` from its own directory, and thirteen samples ship a module
    by that name — one left in the cache answers every later ``from _scaffold import …``, which
    is the hole ``test_sample_modules_import.py`` exists to catch, and which this suite would
    otherwise dig for it. Same eviction that suite uses, for the same reason.
    """
    before = list(sys.path)
    sys.path.insert(0, str(_SAMPLE))
    try:
        spec = importlib.util.spec_from_file_location("sample_12_agent", _SAMPLE / "agent.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = before
        for name, loaded in list(sys.modules.items()):
            origin = getattr(loaded, "__file__", None)
            if origin and Path(origin).parent == _SAMPLE:
                del sys.modules[name]


sample = _load_sample()

_CHECK = _ROOT / "scripts" / "check_live_purge_sample.py"
_check_spec = importlib.util.spec_from_file_location("check_live_purge_sample", _CHECK)
assert _check_spec and _check_spec.loader
check = importlib.util.module_from_spec(_check_spec)
_check_spec.loader.exec_module(check)


def _run_act_five() -> tuple[tuple[str, str, str], str]:
    """The act's return value and everything it printed. One run, read twice."""
    stream = io.StringIO()
    with redirect_stdout(stream):
        disposals = asyncio.run(sample.act_five_a_call_that_could_not_be_cleaned())
    return disposals, stream.getvalue()


class TestTheThreePosturesDiffer:
    """The failure is the same in all three. What the framework does about it is not."""

    def test_the_act_reports_disposed_kept_and_failed(self):
        disposals, _ = _run_act_five()
        assert disposals == ("disposed", "kept", "failed")

    def test_the_default_posture_disposes_and_the_opt_down_does_not(self):
        """The distinction the disposal word alone cannot carry: a `ReclaimFailure` is reported
        under every posture, so what separates them is whether a disposal was asked for."""
        _, printed = _run_act_five()

        default = check._UNCLEAN_DEFAULT.search(printed)
        kept = check._UNCLEAN_KEPT.search(printed)
        assert default is not None and kept is not None, printed
        assert (default.group(1), default.group(2)) == ("disposed", "1")
        assert (kept.group(1), kept.group(2)) == ("kept", "0")

    def test_the_kept_sandbox_still_holds_what_the_call_wrote(self):
        """The price of opting down, as data rather than as a word.

        Asserted together with the disposal count, because on this backend the read alone does
        not carry it: `InProcessSandboxBackend` hands back the same sandbox whatever was
        disposed, so the file would come back after a disposal too. What makes this line the
        cost of opting down is the 0 beside it.
        """
        _, printed = _run_act_five()

        retained = check._RETAINED.search(printed)
        kept = check._UNCLEAN_KEPT.search(printed)
        assert retained is not None and kept is not None, printed
        assert (retained.group(1), kept.group(2)) == (sample.NOTE, "0")

    def test_a_disposal_that_did_not_land_refuses_the_next_call(self):
        """The one consequence that reaches a caller with no callback wired at all."""
        _, printed = _run_act_five()

        failed = check._UNCLEAN_FAILED.search(printed)
        assert failed is not None and failed.group(1) == "failed", printed
        assert check._CLOSED in printed


class TestWhatTheActPrintsIsWhatTheCheckReads:
    """The drift this pair exists to catch: a line reworded here and matched there."""

    def test_the_checks_act_five_patterns_all_find_their_line(self):
        _, printed = _run_act_five()

        assert check._assess_the_call_that_could_not_be_cleaned(printed) == []

    def test_each_of_those_lines_is_printed_once(self):
        """Two lines of one shape and the check reads the first, which is not the truer one."""
        _, printed = _run_act_five()

        assert check._assess_each_line_appears_once(printed) == []
