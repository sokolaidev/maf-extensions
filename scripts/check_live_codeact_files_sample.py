"""Assert that a live run of `samples/08_docker_codeact_files` moved files in both directions.

The other CodeAct checker (`check_live_codeact_sample.py`) reads stdout alone, because samples
03 and 06 produce nothing else. This sample's whole subject is a file that leaves the sandbox,
and stdout cannot show that: the model is handed a sentence by the sink and could write that
sentence whether or not anything landed. So this check has two halves and the second is the
point — **a run that prints the right total and lands nothing must go red.**

    python samples/08_docker_codeact_files/agent.py | tee out.txt
    python scripts/check_live_codeact_files_sample.py out.txt samples/08_docker_codeact_files/out/summary.md

In the output: the grand total over the shipped `sales.csv`, a `Disposed N sandbox(es).` line
with N >= 1, and the sample's own `Delivered this turn` line naming `summary.md` — the host's
record of what reached the sink, not a listing of `out/`. On disk: the summary, carrying each
region's own total. It landing as `summary.md` rather than `<run-id>/summary.md` is itself an
assertion, since the guest path and the delivered name are separate fields.

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
_SEPARATOR = "[,\u00a0\u202f ]"


def _number(value: str) -> re.Pattern[str]:
    """Match ``value`` where it is the whole number, not part of a longer token.

    Nothing may abut it: no digit, no sign, no thousands group, and no word character — so
    `840`, `-390`, `1,390`, `390,000` and `1124e3` are all *not* the value.  A trailing `.0`
    and a thousands separator inside the value are accepted, and the trailing guard skips a
    dot rather than refusing one, so `1124.0.` is the number and `1124.05` is not.
    """
    grouped = value if len(value) <= 3 else f"{value[:-3]}{_SEPARATOR}?{value[-3:]}"
    return re.compile(
        rf"(?<![\w.+-])(?<!\d{_SEPARATOR}){grouped}(?:\.0*)?(?!\.?\d)(?!{_SEPARATOR}\d)(?!\w)"
    )


def _regions_reporting_their_own_total(summary: str) -> tuple[set[str], set[str]]:
    """Split the regions into (named and correct, named but not followed by their total).

    Each region's total must appear between that region's name and whatever region is named
    next.  Checking the two independently over the whole file — which is what this replaces —
    passes a summary with every value swapped, since all eight strings are still present.

    Name-before-value is why the sample's task asks for the region in the first column: the
    association is what catches a swap, and it cannot also accept the reverse order without
    accepting swaps again.  Names match on word boundaries, so a row labelled ``northwest``
    is neither ``north`` nor ``west`` — as a substring it would have been read as both.
    """
    lowered = summary.lower()
    mentions = sorted(
        (match.start(), region)
        for region in _REGION_TOTALS
        for match in re.finditer(rf"\b{re.escape(region)}\b", lowered)
    )
    correct: set[str] = set()
    for index, (start, region) in enumerate(mentions):
        end = mentions[index + 1][0] if index + 1 < len(mentions) else len(summary)
        if _number(_REGION_TOTALS[region]).search(summary[start:end]):
            correct.add(region)
    named = {region for _, region in mentions}
    return correct, named - correct


def _delivered_names(reported: str) -> set[str]:
    """The names on the sample's delivery line, as whole names.

    A membership test rather than a substring one: `summary.md.bak` and `not-summary.md` both
    contain the declared name and neither is it, and the pairing that matters — an earlier
    run's file still on disk, this turn delivering something else — would otherwise pass.
    """
    return {name.strip() for name in reported.split(",") if name.strip()}


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
    elif _SUMMARY_NAME not in _delivered_names(delivered.group(1)):
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
