"""Assert that a live `samples/12_purge_lifecycle` run reclaimed what it created.

    python samples/12_purge_lifecycle/agent.py | tee out.txt
    python scripts/check_live_purge_sample.py out.txt   # or: ... | python …

Matches exactly, like the other two model-free checks: the printed numbers are `docker ps`
counts and router return values, not a retelling of them.

Two assertions carry the first four acts and they fail in opposite directions. **Containers left
behind must be 0** — a sample about not leaking that leaks is worse than no sample. And the
purger must find **1** on the never-scoped thread: that is the only line proving the delete path
does something no other disposal moment would have. A purger wired to nothing also finds 0
everywhere, which is why the tidy thread's 0 cannot be the only zero checked.

The counts it reads come from `docker ps -a`, so a container stopped but not removed still
counts as left behind — which is the shape a half-finished purge actually leaves.

**Act 5 is not counted with `docker ps` and cannot be.** It stages a reclaim that refuses, which
no real backend can be told to do, so it runs on the in-process one; what is read there is the
disposal each posture produced, the number of disposals the backend was actually asked for, and
the two consequences a host can see without a callback — the file a kept sandbox still holds,
and the refusal the next call gets when the disposal itself did not land. All three postures are
checked, because the interesting failure is one of them silently behaving like another.

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

#: Act 5: a call whose cleanup refused, under the three postures a host can be in. Each line
#: carries the disposal the host was told about and how many disposals the backend was asked
#: for — the second is what separates "the framework disposed it" from "the host opted down",
#: which the first alone cannot, since a `ReclaimFailure` is reported either way.
_UNCLEAN_DEFAULT = re.compile(
    r"default posture\s+-> disposal=(\w+), and the backend was asked to dispose it\s+(\d+)",
    re.IGNORECASE,
)
_UNCLEAN_KEPT = re.compile(
    r"keep_unclean=True\s+-> disposal=(\w+), and the backend was asked to dispose it\s+(\d+)",
    re.IGNORECASE,
)
_UNCLEAN_FAILED = re.compile(r"a disposal that fails\s+-> disposal=(\w+)", re.IGNORECASE)

#: What the kept sandbox still held, read back through the conversation's next acquire. This is
#: the retention the default posture exists to prevent, and the only place the sample shows it
#: as data rather than as a word.
_RETAINED = re.compile(r"read the call's file back:\s*'([^']*)'", re.IGNORECASE)
_NOTE = "left behind"

#: The refusal the *next* call reads when the disposal did not land — the one consequence of an
#: unclean sandbox that reaches a caller with no callback wired at all. Matched on the library's
#: own words rather than the whole sentence, which is long and is the library's to reword.
_CLOSED = "the sandbox for this conversation is closed"

#: The footer, every number read back from what the run observed.
_FOOTER = re.compile(
    r"Completed\s+(\d+)\s+of\s+5\s+acts\.\s+Purger found\s+(\d+)\s+on a purged thread and\s+"
    r"(\d+)\s+on an unscoped one\.\s+The three unclean postures reported\s+"
    r"(\w+),\s*(\w+),\s*(\w+)\.\s+Containers left behind:\s*(\d+)\.",
    re.IGNORECASE,
)


#: Each pattern with what it answers for, so a repeated line can be named. Every one reports a
#: single act of a fixed five-act run, and this stream carries no model prose to confuse them.
_SINGULAR = (
    ("the reuse count", _REUSE_COUNT),
    ("the containers kept", _KEPT),
    ("what the scope disposed", _SCOPE_DISPOSED),
    ("what docker had left after the scope", _SCOPE_REMAINING),
    ("what the per-turn purge found", _TIDY),
    ("the unscoped containers before the purge", _UNSCOPED_BEFORE),
    ("what the unscoped purge found", _UNSCOPED_FOUND),
    ("the unscoped containers after the purge", _UNSCOPED_AFTER),
    ("the default posture's unclean call", _UNCLEAN_DEFAULT),
    ("the kept posture's unclean call", _UNCLEAN_KEPT),
    ("the failed disposal's unclean call", _UNCLEAN_FAILED),
    ("what the kept sandbox still held", _RETAINED),
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

    failures.extend(_assess_the_call_that_could_not_be_cleaned(output))
    failures.extend(_assess_each_line_appears_once(output))
    failures.extend(_assess_footer(output))
    return failures


def _assess_the_call_that_could_not_be_cleaned(output: str) -> list[str]:
    """Act 5: one failure, three postures, and they must not report the same thing."""
    failures: list[str] = []

    default = _UNCLEAN_DEFAULT.search(output)
    if default is None:
        failures.append(
            "act 5 did not report the default posture — the framework disposing a sandbox it "
            "could not clean is what every host gets without asking for it"
        )
    elif (default.group(1), int(default.group(2))) != ("disposed", 1):
        failures.append(
            f"the default posture reported disposal={default.group(1)} after "
            f"{default.group(2)} disposal(s), expected disposed after exactly 1 — the "
            "`ReclaimFailure` arrives under every posture, so the count is what tells them apart"
        )

    kept = _UNCLEAN_KEPT.search(output)
    if kept is None:
        failures.append("act 5 did not report the keep_unclean posture — the opt-down is unshown")
    elif (kept.group(1), int(kept.group(2))) != ("kept", 0):
        failures.append(
            f"keep_unclean=True reported disposal={kept.group(1)} after {kept.group(2)} "
            "disposal(s), expected kept after exactly 0 — an opt-down that disposes anyway is "
            "the opposite of what the host asked for"
        )

    retained = _one(_RETAINED, output)
    if retained is None:
        failures.append(
            "act 5 did not read back what the kept sandbox still held — without that line the "
            "cost of opting down is a word rather than a file"
        )
    elif retained != _NOTE:
        failures.append(
            f"the kept sandbox handed back {retained!r} where the call wrote {_NOTE!r} — the "
            "read did not reach the file the refused removal left behind"
        )

    failed = _one(_UNCLEAN_FAILED, output)
    if failed is None:
        failures.append("act 5 did not report a disposal that failed — the refusal path is unshown")
    elif failed != "failed":
        failures.append(
            f"a disposal that could not land reported disposal={failed}, expected failed"
        )
    elif _CLOSED not in output:
        failures.append(
            "the call after a failed disposal was not refused — a router that goes on serving a "
            "key it could not clean is the one outcome worse than a failed conversation"
        )

    return failures


def _assess_footer(output: str) -> list[str]:
    footer = _FOOTER.search(output)
    if footer is None:
        return ["no footer line — the sample did not run to completion"]
    acts, tidy, unscoped = (int(group) for group in footer.group(1, 2, 3))
    postures = footer.group(4, 5, 6)
    leftover = int(footer.group(7))
    failures: list[str] = []
    if acts != 5:
        failures.append(f"only {acts} of 5 acts completed — the sample stopped part-way")
    if (tidy, unscoped) != (0, 1):
        failures.append(
            f"the footer reports {tidy} and {unscoped} where the acts reported 0 and 1 — the "
            "summary and the run disagree"
        )
    if postures != ("disposed", "kept", "failed"):
        failures.append(
            f"the footer reports the unclean postures as {', '.join(postures)} where act 5 "
            "reported disposed, kept and failed — the summary and the run disagree"
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
        "an unclean call disposed, kept and refused under the three postures"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
