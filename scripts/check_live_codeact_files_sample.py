"""Assert that a live run of `samples/08_docker_codeact_files` moved files in both directions.

The other CodeAct checker (`check_live_codeact_sample.py`) reads stdout alone, because samples
03 and 06 produce nothing else. This sample's whole subject is a file that leaves the sandbox,
and stdout cannot show that: the model is handed a sentence by the sink and could write that
sentence whether or not anything landed. So this check has two halves and the second is the
point — **a run that prints the right total and lands nothing must go red.**

    python samples/08_docker_codeact_files/agent.py | tee out.txt
    python scripts/check_live_codeact_files_sample.py out.txt samples/08_docker_codeact_files/out/summary.md

What it checks in the output: the grand total `1124` over the shipped `sales.csv`, a
`Disposed N sandbox(es).` line with N >= 1, and the sample's own `Delivered this turn` line
naming `summary.md`. That last line is the host's record of what reached the sink during this
turn — not a listing of `out/`, which would still name an earlier run's file.

What it checks on disk: the summary exists, is not empty, and reports each region's own total.
The file landing as `summary.md` rather than `<run-id>/summary.md` is itself an assertion: the
guest path and the delivered name are separate fields, and this is where that shows.

Numbers are matched as whole tokens rather than substrings, so `840` does not satisfy `84` and
`11240` does not satisfy `1124`, and each region's total has to appear before the next region
is named, so a summary that swaps two regions' values fails. Both allowances are deliberate: a
trailing `.0` passes, because a program that computed in floats is not what is under test, and
so does a thousands separator, because the grand total is read out of the *model's* prose and
`1,124` is a formatting choice rather than a broken stack.

The two halves are weak apart and strong together. A stale `summary.md` from an earlier run
would satisfy the on-disk half alone, which is why the workflow deletes the directory first and
why the delivery line is the host's record rather than a listing; a model reciting a total it
inferred would satisfy the output half alone.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The grand total over the `sales.csv` that ships beside the sample. The task has exactly one
#: right answer, so the literal is required.
_GRAND_TOTAL = "1124"

#: Every region in the file, and what the summary must report for each.
_REGION_TOTALS = {"north": "390", "south": "200", "east": "84", "west": "450"}

#: What the declared output must be called once delivered — not `<run-id>/summary.md`.
_SUMMARY_NAME = "summary.md"

_DISPOSED = re.compile(r"Disposed\s+(\d+)\s+sandbox", re.IGNORECASE)
_DELIVERED = re.compile(
    r"^Delivered this turn[^:]*:\s*(.+)$", re.IGNORECASE | re.MULTILINE
)


#: What a model may put between thousands: a comma, a plain space, a no-break space or a
#: narrow one. Optional, because the program itself prints none of them.
_THOUSANDS = "[,   ]?"


def _number(value: str) -> re.Pattern[str]:
    """Match ``value`` as a whole number, tolerating `.0` and a thousands separator.

    A substring test is what this replaces, and it was wrong in both directions: `84` was
    satisfied by `840` and `1124` by `11240`, so a summary carrying the wrong magnitude passed.
    """
    grouped = value if len(value) <= 3 else f"{value[:-3]}{_THOUSANDS}{value[-3:]}"
    # The trailing guard rejects a digit, not a dot: `1124.0.` ends a sentence and is
    # still the number, while `1124.05` is a different one.
    return re.compile(rf"(?<![\d.]){grouped}(?:\.0*)?(?!\.?\d)")


def _regions_reporting_their_own_total(summary: str) -> tuple[set[str], set[str]]:
    """Split the regions into (named and correct, named but not followed by their total).

    Each region's total must appear between that region's name and whatever region is named
    next.  Checking the two independently over the whole file — which is what this replaces —
    passes a summary with every value swapped, since all eight strings are still present.
    """
    lowered = summary.lower()
    mentions = sorted(
        (match.start(), region)
        for region in _REGION_TOTALS
        for match in re.finditer(re.escape(region), lowered)
    )
    correct: set[str] = set()
    for index, (start, region) in enumerate(mentions):
        end = mentions[index + 1][0] if index + 1 < len(mentions) else len(summary)
        if _number(_REGION_TOTALS[region]).search(summary[start:end]):
            correct.add(region)
    named = {region for _, region in mentions}
    return correct, named - correct


def assess(output: str, summary: str | None) -> list[str]:
    """Return every reason this was not a healthy run — empty means it passed.

    ``summary`` is the landed file's text, or ``None`` when it is not there at all. Kept as an
    argument rather than read here so the whole judgement stays a pure function.
    """
    failures: list[str] = []

    if not _number(_GRAND_TOTAL).search(output):
        failures.append(
            f"{_GRAND_TOTAL} is not in the output as a number — the grand total over sales.csv "
            "did not come back, so the program did not read the file it was given"
        )

    disposed = _DISPOSED.search(output)
    if disposed is None:
        failures.append(
            "no 'Disposed N sandbox(es)' line — the sample did not run to completion"
        )
    elif int(disposed.group(1)) < 1:
        failures.append(
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so nothing ran in one"
        )

    delivered = _DELIVERED.search(output)
    if delivered is None:
        failures.append(
            "no 'Delivered this turn' line — the sample did not reach its final report"
        )
    elif _SUMMARY_NAME not in delivered.group(1):
        failures.append(
            f"the host recorded delivering {delivered.group(1).strip()!r}, which does not "
            f"include {_SUMMARY_NAME!r} — a declared output did not reach the sink this turn"
        )

    if summary is None:
        failures.append(
            f"{_SUMMARY_NAME} is not on disk — the turn may have answered correctly and saved "
            "nothing, which is the failure this sample exists to catch"
        )
        return failures

    if not summary.strip():
        failures.append(f"{_SUMMARY_NAME} landed empty")
        return failures

    correct, wrong = _regions_reporting_their_own_total(summary)
    for region in _REGION_TOTALS:
        if region in wrong:
            failures.append(
                f"{_SUMMARY_NAME} names the {region} region but not its total of "
                f"{_REGION_TOTALS[region]} before the next region — a swapped or wrong value"
            )
        elif region not in correct:
            failures.append(f"{_SUMMARY_NAME} does not mention the {region} region")

    return failures


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <output-file> <landed-summary-path>", file=sys.stderr)
        return 2

    output = Path(argv[1]).read_text(encoding="utf-8")
    landed_path = Path(argv[2])
    summary = landed_path.read_text(encoding="utf-8") if landed_path.is_file() else None

    failures = assess(output, summary)
    if failures:
        print(
            "FAIL: the live sample run did not verify the published stack:",
            file=sys.stderr,
        )
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(
        "OK  the CodeAct sample read a workspace file in a live container and landed its "
        "declared summary against the published wheels"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
