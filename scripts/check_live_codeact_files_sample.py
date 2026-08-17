"""Assert that a live CodeAct files run moved files in both directions.

Shared by `samples/08_docker_codeact_files` (a Docker container on this machine) and
`samples/14_acas_codeact_files` (a real Azure sandbox) — the task, the data, the one right
answer and the printed shape are identical, so one checker serves both and a red on one side
alone names the backend rather than the workload (#300).

    python samples/08_docker_codeact_files/agent.py | tee out.txt
    python scripts/check_live_codeact_files_sample.py out.txt samples/08_docker_codeact_files/out/summary.md

Two halves, and the second is the point: stdout cannot show that a file left the sandbox, so a
run that prints the right total and lands nothing must go red. The landed summary is the half
that cannot be written by a model — the sink is host-side code, and the only road to `out/` runs
through a program that actually ran and an artifact actually pulled back.

Two of the three things this reads out of the transcript are the host's own — what
`dispose_scope` returned, and what the sink took this turn — and both are tagged `[measured]`
for the reason #314 set out: the model answers into the same stream, so an unmarked search finds
a reply saying "Disposed 1 sandbox(es)." before the sample's own line. The sample takes the tag
away from anything the model said before printing either. **Both halves of that are
load-bearing**: `quoted` defangs a tag only at the start of a line, so the `^` anchoring these
patterns is the only thing refusing one written mid-sentence.

The third — the grand total — is read from the transcript at large, which in a healthy run means
out of the model's own reply, because these two samples print no fenced block of the tool's
output. That is deliberate and it is not the gate. It is the same claim `check_live_codeact_sample.py`
makes with its reply check: the answer has to reach the model and not merely the log. What
*proves* a program ran over the real file is the landed summary, where every region's total has
to sit against that region's name — and those four numbers are the grand total, decomposed. A
model that never called the tool cannot produce them.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import json
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

#: Both lines below come off one the *sample* tagged. `MEASURED` in `samples/*/_scaffold.py`
#: writes the tag and `quoted` there takes it away from anything the model said, so a reply
#: impersonating either line is a quotation by the time this reads the stream. Case-sensitive on
#: the tag, lax after it: a reader broader than its sanitizer is a hole rather than tolerance.
#:
#: The `^` is not decoration. `quoted` tests `line.lstrip().startswith(...)`, so it rewrites a
#: tag that opens a line and leaves one buried in a sentence exactly as the model wrote it —
#: `All done!   [measured] Disposed 1 sandbox(es).` reaches this unchanged. The anchor is the
#: whole of what refuses it, and it is pinned by a test of its own rather than by the
#: impersonation cases, which cannot reach a mid-line tag at all.
_M = r"^  (?-i:\[measured\]) "
_F = re.MULTILINE | re.IGNORECASE

_DISPOSED = re.compile(_M + r"Disposed\s+(\d+)\s+sandbox", _F)

#: `[^:\n]` and `[ \t]` rather than `[^:]` and `\s`, both of which cross a line break: were the
#: sample's own line ever to lose its colon, the greedy walk would find the next one further
#: down the stream and take its capture from whatever the model wrote there. The line has a
#: colon today; a pattern that reads the host's line should not be able to read anything else.
_DELIVERED = re.compile(_M + r"Delivered this turn[^:\n]*:[ \t]*(.+)$", _F)


#: What a model may put between thousands: a comma, a plain space, a no-break space or a
#: narrow one. Optional, because the program itself prints none of them.
_SEPARATOR = "[,\u00a0\u202f ]"

#: Signs a model may render, beyond the ASCII pair: the true minus, and the fullwidth
#: forms. This checker reads model-authored prose, where `\u2212390` is as likely as `-390`.
_SIGNS = "+\u2212\uff0b\uff0d-"


def _number(value: str) -> re.Pattern[str]:
    """Match ``value`` where it is the whole number, not part of a longer token.

    Nothing may abut it: no digit, no sign (ASCII or Unicode), no thousands group, and no
    word character — so `840`, `-390`, `\u2212390`, `1,390`, `390,000` and `1124e3` are all
    *not* the value.  A trailing `.0`
    and a thousands separator inside the value are accepted, and the trailing guard skips a
    dot rather than refusing one, so `1124.0.` is the number and `1124.05` is not.
    """
    grouped = value if len(value) <= 3 else f"{value[:-3]}{_SEPARATOR}?{value[-3:]}"
    return re.compile(
        rf"(?<![\w.{_SIGNS}])(?<!\d{_SEPARATOR}){grouped}"
        rf"(?:\.0*)?(?!\.?\d)(?!{_SEPARATOR}\d)(?!\w)"
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

    Matched case-insensitively against ``summary`` itself rather than against a lowered copy.
    Lowering is not length-preserving — ``"İ".lower()`` is two characters — so a summary
    containing one would have shifted every offset after it and sliced the segments apart from
    the text they were found in.  The model writes this file, so its alphabet is not ours to
    assume.
    """
    mentions = sorted(
        (match.start(), region)
        for region in _REGION_TOTALS
        for match in re.finditer(rf"\b{re.escape(region)}\b", summary, re.IGNORECASE)
    )
    correct: set[str] = set()
    for index, (start, region) in enumerate(mentions):
        end = mentions[index + 1][0] if index + 1 < len(mentions) else len(summary)
        if _number(_REGION_TOTALS[region]).search(summary[start:end]):
            correct.add(region)
    named = {region for _, region in mentions}
    return correct, named - correct


def _delivered_names(reported: str) -> set[str]:
    """The names on the sample's delivery line, exactly as the host recorded them.

    The sample emits JSON so this can be read rather than guessed at: an artifact name may
    legally contain a comma, and splitting one delivery called ``notes, summary.md`` yields
    two, one of them the declared name. An unparseable line is no names, which fails.
    """
    try:
        reported_names = json.loads(reported)
    except ValueError:
        return set()
    if not isinstance(reported_names, list):
        return set()
    return {name for name in reported_names if isinstance(name, str)}


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
            "no measured 'Disposed N sandbox(es)' line — the sample did not run to completion"
        )
    elif int(disposed.group(1)) < 1:
        failures.append(
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so nothing ran in one"
        )

    delivered = _DELIVERED.search(output)
    if delivered is None:
        failures.append(
            "no measured 'Delivered this turn' line — the sample did not reach its final report"
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
    """CLI entry: read the sample output and the landed summary, run ``assess``, and print OK or FAIL."""
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
        "OK  the CodeAct sample read a file from the store in a live sandbox and landed its "
        "declared summary against the published wheels"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
