"""Assert that a live `samples/12_docker_purge_lifecycle` run reclaimed what it created.

    python samples/12_docker_purge_lifecycle/agent.py | tee out.txt
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

#: Act 3: what the router reported disposing when the `scope` block ended, and what `docker ps`
#: saw afterwards. The first is the library's claim, the second is the machine's answer.
_SCOPE_DISPOSED = re.compile(r"block ended -> router reports\s+(\d+)\s+disposed", re.IGNORECASE)
_SCOPE_REMAINING = re.compile(r"and docker agrees -> containers:\s*(\d+)", re.IGNORECASE)

#: Act 4, both threads, plus the container count before the delete path runs on the
#: never-scoped one — without that the purger's 1 could be a number it made up.
_TIDY = re.compile(r"already purged per turn -> purger found\s+(\d+)", re.IGNORECASE)
_UNSCOPED_BEFORE = re.compile(r"never scoped per turn\s+-> containers:\s*(\d+)", re.IGNORECASE)
_UNSCOPED_FOUND = re.compile(r"deletes the conversation\s+-> purger found\s+(\d+)", re.IGNORECASE)

#: The footer, all three numbers read back from what the run observed.
_FOOTER = re.compile(
    r"Completed\s+(\d+)\s+of\s+4\s+acts\.\s+Purger found\s+(\d+)\s+on a purged thread and\s+"
    r"(\d+)\s+on an unscoped one\.\s+Containers left behind:\s*(\d+)\.",
    re.IGNORECASE,
)


def _one(pattern: re.Pattern[str], output: str) -> str | None:
    match = pattern.search(output)
    return match.group(1) if match else None


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
            f"act 1 left {reuse_count} containers running for one key, expected exactly 1 — two "
            "acquires that each created one would still have served the file back"
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

    failures.extend(_assess_footer(output))
    return failures


def _assess_footer(output: str) -> list[str]:
    footer = _FOOTER.search(output)
    if footer is None:
        return ["no footer line — the sample did not run to completion"]
    acts, tidy, unscoped, leftover = (int(group) for group in footer.groups())
    failures: list[str] = []
    if acts != 4:
        failures.append(f"only {acts} of 4 acts completed — the sample stopped part-way")
    if (tidy, unscoped) != (0, 1):
        failures.append(
            f"the footer reports {tidy} and {unscoped} where the acts reported 0 and 1 — the "
            "summary and the run disagree"
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
    print("OK  reuse within a turn, disposal at its end, and the delete path catching the rest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
