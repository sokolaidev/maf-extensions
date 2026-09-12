"""Sample 12's reclaim-failure handler, driven with no container and no engine.

The sample itself runs the handler against a real Docker reclaim, which is what makes its acts
worth anything — but it runs at one redaction setting, and the live check can only read the
posture the run chose. What crosses to a collector at the *other* setting is a decision with a
deployment's confidentiality behind it, so it is asserted here rather than left to the setting
nobody runs.

`record_sensitive_data` is the package's own switch and the handler honours it rather than
inventing a second one. These tests pin which of the four attributes it governs: the two that
carry host-chosen strings move with it, and the two a host branches on do not.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from maf_sandbox import ReclaimFailure, SandboxKey

_SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "12_purge_lifecycle"
sys.path.insert(0, str(_SAMPLE))

from telemetry import (  # noqa: E402
    CALL_ID,
    DISPOSAL,
    PATH,
    REASON,
    RECLAIM_FAILURE_SPAN,
    build_telemetry,
    exported,
)

_KEY = SandboxKey(scope="samples", thread_id="t-locked", agent_id="assistant")
_CALL = "0" * 32


def _recorded(*, sensitive: bool, path: str = _CALL) -> dict[str, object]:
    """Run one failure through the handler and return the attributes its span carried."""
    telemetry = build_telemetry(record_sensitive_data=sensitive)
    failure = ReclaimFailure(
        tool="leave_a_locked_directory",
        key=_KEY,
        path=path,
        reason="the removal call failed: OSError: rm exited 1 - Permission denied",
        disposal="disposed",
    )
    asyncio.run(telemetry.on_reclaim_failure(failure))
    (span,) = exported(telemetry.exporter, RECLAIM_FAILURE_SPAN)
    return dict(span.attributes or {})


class TestWhatCrossesToACollector:
    def test_the_host_opting_in_gets_the_path_and_the_reason(self):
        recorded = _recorded(sensitive=True)
        assert recorded[PATH] == _CALL
        assert "Permission denied" in str(recorded[REASON])

    def test_the_default_posture_withholds_both(self):
        """Neither is a value a host branches on, and both name infrastructure.

        The path is a directory inside somebody's sandbox and the reason quotes the engine, so
        they cross on the same switch the package holds its own detail behind.
        """
        recorded = _recorded(sensitive=False)
        assert PATH not in recorded
        assert REASON not in recorded

    def test_the_disposal_outcome_crosses_either_way(self):
        """The first thing a host branches on, and it names nothing.

        Withholding it would leave a record that says a cleanup failed and not what was done
        about it, which is the one fact the callback exists to deliver.
        """
        for sensitive in (True, False):
            assert _recorded(sensitive=sensitive)[DISPOSAL] == "disposed"

    def test_the_call_id_crosses_either_way(self):
        """Generated per call, names nobody, and is what joins this record to the others.

        Held back with the rest it would cost the correlation and protect nothing — the same
        reasoning `Redaction` applies to the key's own call id.
        """
        for sensitive in (True, False):
            assert _recorded(sensitive=sensitive)[CALL_ID] == _CALL


class TestACallThatNamedNoPath:
    def test_a_whole_base_path_carries_no_call_id(self):
        """`ReclaimFailure.path` is `"."` when the body never asked for a path of its own.

        There is no call directory, so there is no call id in it either, and stamping the join
        column with `"."` would group every such record together as one call.
        """
        recorded = _recorded(sensitive=True, path=".")
        assert CALL_ID not in recorded
        assert recorded[PATH] == "."
