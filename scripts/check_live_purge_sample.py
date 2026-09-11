"""Assert that a live `samples/12_purge_lifecycle` run reclaimed what it created.

    python samples/12_purge_lifecycle/agent.py | tee out.txt
    python scripts/check_live_purge_sample.py out.txt   # or: ... | python …

Matches exactly, like the other two model-free checks: the printed numbers are `docker ps`
counts and router return values, not a retelling of them.

Two assertions carry the sample and they fail in opposite directions. **Containers left behind
must be 0** — a sample about not leaking that leaks is worse than no sample. And the purger
must find **1** on the never-scoped thread: that is the only line proving the delete path
does something no other disposal moment would have. A purger wired to nothing also finds 0
everywhere, which is why the tidy thread's 0 cannot be the only zero checked.

The counts it reads come from `docker ps -a`, so a container stopped but not removed still
counts as left behind — which is the shape a half-finished purge actually leaves.

Acts 5 and 6 add a third direction. They make a reclaim **fail for real** and assert what each
policy then did: the default disposes the sandbox it could not clean, so the container count goes
to nought, and `FailedReclaimPolicy.KEEP` leaves it at one with the data still in it. Both are
read off `docker ps` like everything else here. Beside them the check reads the telemetry the run
exported, because the handler firing is the whole point of those acts — a healthy run reports
nothing, so a count of nought would pass while proving nothing (#760).

One assertion there looks like a tautology and is not: `maf_sandbox.call.unclean` must read `0`
beside a directory that was not removed. That attribute counts what a transport noted about
processes, never a failed removal, and the sample's whole argument is built on the two being
separate. If core ever folds them together this goes red — correctly, because the sample's prose
would then be wrong and the host record it argues for would be redundant.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Act 1: state written through one `acquire` handle, read back through the next. That is what
#: reuse means to a workload — the same *sandbox*, which the protocol promises, rather than the
#: same Python object, which it does not and which backends do differently. Paired with the
#: container count, because a second container would also serve a file if both were written to.
_REUSED_STATE = "still here"
_REUSE_COUNT = re.compile(r"containers for this thread:\s*(\d+)", re.IGNORECASE)

#: Act 2: the sandbox is still there after the turn. The premise the whole cost argument rests
#: on — if a turn's sandbox did not survive it, there would be nothing to decide about.
_KEPT = re.compile(r"containers still there:\s*(\d+)", re.IGNORECASE)

#: Act 3: what the router reported disposing when the `scope` block ended, and what `docker ps`
#: saw afterwards. The first is the library's claim, the second is the machine's answer.
_SCOPE_DISPOSED = re.compile(r"block ended -> router reports\s+(\d+)\s+disposed", re.IGNORECASE)
_SCOPE_REMAINING = re.compile(r"and docker agrees -> containers:\s*(\d+)", re.IGNORECASE)

#: Act 4: both threads, and the container count either side of the delete path. `_UNSCOPED_AFTER`
#: keys on wording act 3 does not share, so the two post-purge lines cannot be confused.
_TIDY = re.compile(r"already purged per turn -> purger found\s+(\d+)", re.IGNORECASE)
_UNSCOPED_BEFORE = re.compile(r"never scoped per turn\s+-> containers:\s*(\d+)", re.IGNORECASE)
_UNSCOPED_FOUND = re.compile(r"deletes the conversation\s+-> purger found\s+(\d+)", re.IGNORECASE)
_UNSCOPED_AFTER = re.compile(r"after purge\s+-> containers:\s*(\d+)", re.IGNORECASE)

#: Act 5: the precondition, and the escalation. The rung has to be `reclaim` or there is no
#: per-call removal to fail and the act proves nothing; the count is what the disposal did.
_RUNG = re.compile(r"Cleanup rung for this call:\s*(\S+)", re.IGNORECASE)
_ESCALATED = re.compile(r"containers after the escalation:\s*(\d+)", re.IGNORECASE)

#: Act 6: the opt-down, measured the same way. One container, still holding the data.
_KEPT_UNCLEAN = re.compile(r"containers kept after the failure:\s*(\d+)", re.IGNORECASE)

#: Both acts, in order: act 5 first, act 6 second. Two lines each, so these are read as ordered
#: pairs rather than single answers — which is also why they are not in `_SINGULAR`.
_CALL_SPAN = re.compile(r"sandbox\.call\s+maf_sandbox\.call\.unclean = (\S+)")
_DISPOSE_SPAN = re.compile(
    r"sandbox\.dispose\s+(\d+) record\(s\), maf_sandbox\.disposal\.outcome = (\S+)"
)
_RECORDS = re.compile(r"Disposal records for this call:\s*(\d+)", re.IGNORECASE)
_RECORDED_DISPOSAL = re.compile(r"Recorded disposal:\s*(\S+)", re.IGNORECASE)
_RECORDED_REASON = re.compile(r"Recorded reason:\s*(.+)")
#: The call each act's records were selected by. Two distinct ones, or the second act read the
#: first act's records and every value below it is the wrong act's.
_COLLECTED_FOR = re.compile(r"what a collector received for call\s+([0-9a-f]{32}):", re.IGNORECASE)

#: What the handler writes when the framework could not tell it a path, and what the sample
#: prints for a record that was never made. Either one in a reason is a failed assertion.
_NOT_RECORDED = ("(absent)", "(no record)")

#: The footer, every number read back from what the run observed.
_FOOTER = re.compile(
    r"Completed\s+(\d+)\s+of\s+6\s+acts\.\s+Purger found\s+(\d+)\s+on a purged thread and\s+"
    r"(\d+)\s+on an unscoped one\.\s+Kept after a failed reclaim:\s*(\d+)\.\s+"
    r"Reclaim failures recorded:\s*(\d+)\.\s+Containers left behind:\s*(\d+)\.",
    re.IGNORECASE,
)


#: Each pattern with what it answers for, so a repeated line can be named. Every one reports a
#: single act of a fixed six-act run, and this stream carries no model prose to confuse them.
_SINGULAR = (
    ("the reuse count", _REUSE_COUNT),
    ("the containers kept", _KEPT),
    ("what the scope disposed", _SCOPE_DISPOSED),
    ("what docker had left after the scope", _SCOPE_REMAINING),
    ("what the per-turn purge found", _TIDY),
    ("the unscoped containers before the purge", _UNSCOPED_BEFORE),
    ("what the unscoped purge found", _UNSCOPED_FOUND),
    ("the unscoped containers after the purge", _UNSCOPED_AFTER),
    ("the cleanup rung", _RUNG),
    ("the containers after the escalation", _ESCALATED),
    ("the containers kept after the failure", _KEPT_UNCLEAN),
    ("the footer", _FOOTER),
)


def _one(pattern: re.Pattern[str], output: str) -> str | None:
    match = pattern.search(output)
    return match.group(1) if match else None


def _assess_each_line_appears_once(output: str) -> list[str]:
    """A second line of the same shape is a second answer, and the first is not the truer one."""
    return [
        f"{what} is reported on {count} lines, so none of them can be trusted — the sample "
        "reports it once"
        for what, pattern in _SINGULAR
        if (count := len(pattern.findall(output))) > 1
    ]


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    if _REUSED_STATE not in output:
        failures.append(
            f"act 1 did not read {_REUSED_STATE!r} back through the second acquire — the second "
            "acquire reached a different sandbox, so get-or-create did not hold and every "
            "disposal argument below assumes it does"
        )
    reuse_count = _one(_REUSE_COUNT, output)
    if reuse_count is None:
        failures.append("act 1 did not report its container count")
    elif int(reuse_count) != 1:
        failures.append(
            f"act 1 left {reuse_count} container(s) for one key, expected exactly 1 — the file "
            "coming back already proves the second acquire reached the first one's sandbox, so "
            "what this catches is a container created and then orphaned beside it"
        )

    kept = _one(_KEPT, output)
    if kept is None:
        failures.append("act 2 did not report whether the sandbox survived the turn")
    elif int(kept) != 1:
        failures.append(
            f"{kept} container(s) after a turn that did not dispose, expected exactly 1 — a "
            "sandbox that does not outlive its turn leaves nothing for the rest of this sample "
            "to decide about"
        )

    disposed = _one(_SCOPE_DISPOSED, output)
    remaining = _one(_SCOPE_REMAINING, output)
    if disposed is None or remaining is None:
        failures.append("act 3 did not report the scope block's disposal — `router.scope` unshown")
    else:
        if int(disposed) != 1:
            failures.append(
                f"the scope block reported disposing {disposed}, expected exactly 1 — the turn "
                "inside it acquired one sandbox"
            )
        if int(remaining) != 0:
            failures.append(
                f"{remaining} container(s) still running after the scope block — disposal on "
                "block exit is the whole mechanism act 3 exists to show"
            )

    tidy = _one(_TIDY, output)
    if tidy is None:
        failures.append("act 4 did not report the purger's result on the already-purged thread")
    elif int(tidy) != 0:
        failures.append(
            f"the purger found {tidy} on a thread already purged per turn, expected exactly 0 — "
            "either the per-turn purge did not reclaim, or the two are counting the same "
            "sandbox twice"
        )

    before = _one(_UNSCOPED_BEFORE, output)
    found = _one(_UNSCOPED_FOUND, output)
    if before is None or found is None:
        failures.append("act 4 did not report the never-scoped thread — the backstop is unshown")
    else:
        if int(before) != 1:
            failures.append(
                f"the never-scoped thread had {before} container(s) before the delete path, "
                "expected exactly 1 — with nothing there the purger's result proves nothing"
            )
        if int(found) != 1:
            failures.append(
                f"the purger found {found} on the never-scoped thread, expected exactly 1 — this "
                "is the only line showing the delete path reclaiming something no other disposal "
                "moment would have, and a purger wired to nothing also reports 0"
            )

    after = _one(_UNSCOPED_AFTER, output)
    if after is None:
        failures.append(
            "act 4 did not report the container count after the purge — the purger's own number "
            "is its claim, and nothing else here checks the machine before the final sweep"
        )
    elif int(after) != 0:
        failures.append(
            f"{after} container(s) still there after the delete path ran — the purger reported "
            "reclaiming and did not, which the footer cannot see because `main` sweeps every "
            "thread before computing it"
        )

    failures.extend(_assess_the_cleanup_that_failed(output))
    failures.extend(_assess_each_line_appears_once(output))
    failures.extend(_assess_footer(output))
    return failures


def _assess_the_cleanup_that_failed(output: str) -> list[str]:
    """Acts 5 and 6: a reclaim that really failed, what each policy did, and what was recorded."""
    failures: list[str] = []

    rung = _one(_RUNG, output)
    if rung is None:
        failures.append("act 5 did not report the cleanup rung it ran at")
    elif rung != "reclaim":
        failures.append(
            f"the cleanup rung was {rung!r}, expected 'reclaim' — at any other rung the call's "
            "own directory is never removed on its own, so nothing in acts 5 and 6 was exercised "
            "and their passing numbers would mean nothing"
        )

    escalated = _one(_ESCALATED, output)
    if escalated is None:
        failures.append("act 5 did not report the container count after the escalation")
    elif int(escalated) != 0:
        failures.append(
            f"{escalated} container(s) after a reclaim that failed, expected 0 — the framework "
            "disposes a sandbox it could not clean, and one left running is the leak the "
            "escalation exists to prevent"
        )

    kept_unclean = _one(_KEPT_UNCLEAN, output)
    if kept_unclean is None:
        failures.append("act 6 did not report the container count under FailedReclaimPolicy.KEEP")
    elif int(kept_unclean) != 1:
        failures.append(
            f"{kept_unclean} container(s) under FailedReclaimPolicy.KEEP, expected exactly 1 — "
            "the setting's whole effect is that the sandbox stays, and a 0 here means the two "
            "policies did the same thing"
        )

    failures.extend(_assess_what_was_recorded(output))
    return failures


def _assess_what_was_recorded(output: str) -> list[str]:
    """The telemetry both acts exported, read as ordered pairs: act 5 first, act 6 second.

    This is the half no healthy run reaches. A reclaim that never fails calls no handler, so
    every one of these lines is absent from a sample whose failure stopped being forced — which
    is the silent pass this check exists to make impossible (#760).
    """
    failures: list[str] = []

    disposals = _RECORDED_DISPOSAL.findall(output)
    if disposals != ["disposed", "kept"]:
        failures.append(
            f"the recorded disposals were {disposals}, expected ['disposed', 'kept'] — the "
            "handler runs after the framework has already acted, and these are what it was told "
            "each policy did. Anything else means the handler did not fire for both acts, or "
            "the two policies are no longer distinguishable to a host"
        )

    records = _RECORDS.findall(output)
    if records != ["2", "1"]:
        failures.append(
            f"the disposal record counts were {records}, expected ['2', '1'] — act 5 records the "
            "router's adoption cleanup and the escalation, act 6 only the adoption, and the "
            "sample's argument is that neither act's records say which was which"
        )

    reasons = _RECORDED_REASON.findall(output)
    if len(reasons) != 2:
        failures.append(f"{len(reasons)} recorded reason(s), expected 2 — one per failed cleanup")
    for reason in reasons:
        if reason.strip() in _NOT_RECORDED or len(reason.strip()) < 20:
            failures.append(
                f"a recorded reason was {reason.strip()!r} — `ReclaimFailure.reason` is this "
                "stack's own sentence for what did not happen, and it is one of the three facts "
                "the host record exists to carry"
            )

    unclean = _CALL_SPAN.findall(output)
    if unclean != ["0", "0"]:
        failures.append(
            f"maf_sandbox.call.unclean read {unclean}, expected ['0', '0'] — that attribute "
            "counts what a transport noted about processes it could not prove it stopped, never "
            "a removal that failed. If it now counts these too, this sample's argument for a "
            "host record is out of date and its prose needs rewriting rather than its numbers"
        )

    dispose_spans = _DISPOSE_SPAN.findall(output)
    if [outcome for _, outcome in dispose_spans] != ["gone", "gone"]:
        failures.append(
            f"the disposal outcomes were {[o for _, o in dispose_spans]}, expected "
            "['gone', 'gone'] — a disposal the backend reported as failed is a different story "
            "from the one these acts tell, and the container counts above would not be reliable"
        )

    collected = _COLLECTED_FOR.findall(output)
    if len(collected) != 2:
        failures.append(f"{len(collected)} record set(s) were reported, expected 2 — one per act")
    elif collected[0] == collected[1]:
        failures.append(
            "both acts reported records for the same call id, so act 6 read act 5's records — "
            "the selection is on the call id precisely so the two cannot be confused"
        )

    return failures


def _assess_footer(output: str) -> list[str]:
    footer = _FOOTER.search(output)
    if footer is None:
        return ["no footer line — the sample did not run to completion"]
    acts, tidy, unscoped, kept_unclean, reported, leftover = (
        int(group) for group in footer.groups()
    )
    failures: list[str] = []
    if acts != 6:
        failures.append(f"only {acts} of 6 acts completed — the sample stopped part-way")
    if (tidy, unscoped) != (0, 1):
        failures.append(
            f"the footer reports {tidy} and {unscoped} where the acts reported 0 and 1 — the "
            "summary and the run disagree"
        )
    if kept_unclean != 1:
        failures.append(
            f"the footer reports {kept_unclean} kept after a failed reclaim, expected 1 — the "
            "summary and act 6 disagree"
        )
    if reported != 2:
        failures.append(
            f"{reported} reclaim failure(s) recorded, expected 2 — this is counted off the "
            "exporter rather than kept in a variable, so it is what a collector received and "
            "not what the handler believes it sent. Nought is what this sample existed to stop "
            "passing (#760)"
        )
    if leftover != 0:
        failures.append(
            f"{leftover} container(s) left behind — a sample about reclaiming sandboxes may not "
            "leave one running, and this count is `docker ps`, not a value the run chose"
        )
    return failures


def main(argv: list[str]) -> int:
    """CLI entry: read the sample output from a file or stdin, run ``assess``, print OK or FAIL."""
    if len(argv) > 2:
        print(f"usage: {argv[0]} [output-file]  (reads stdin if omitted)", file=sys.stderr)
        return 2
    output = (
        sys.stdin.read()
        if len(argv) == 1 or argv[1] == "-"
        else Path(argv[1]).read_text(encoding="utf-8")
    )

    failures = assess(output)
    if failures:
        print("FAIL: the purge sample did not reclaim what it created:", file=sys.stderr)
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(
        "OK  reuse within a turn, disposal at its end, the delete path catching the rest, and "
        "a reclaim that failed reported to the host both ways"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
