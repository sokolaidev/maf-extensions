"""Assert that a live run of `samples/08_docker_codeact_files` moved files in both directions.

The other CodeAct checker (`check_live_codeact_sample.py`) reads stdout alone, because samples
03 and 06 produce nothing else. This sample's whole subject is a file that leaves the sandbox,
and stdout cannot show that: the model is handed a sentence by the sink and could write that
sentence whether or not anything landed. So this check has two halves and the second is the
point — **a run that prints the right total and lands nothing must go red.**

    python samples/08_docker_codeact_files/agent.py | tee out.txt
    python scripts/check_live_codeact_files_sample.py out.txt samples/08_docker_codeact_files/out/summary.md

What it checks in the output: the grand total `1124` over the shipped `sales.csv`, a
`Disposed N sandbox(es).` line with N >= 1, and the sample's own `Landed in out/:` line naming
`summary.md`. That last line is printed by the host after the turn, from the sink's own
directory rather than from anything the model said.

What it checks on disk: the summary exists, is not empty, and carries all four regions and all
four per-region totals. Totals are matched as substrings so a program that computed in floats
(`390.0`) passes — the arithmetic is not what is under test here, the channel is. The file
landing as `summary.md` rather than `<run-id>/summary.md` is itself an assertion: the guest
path and the delivered name are separate fields, and this is where that shows.

The two halves are weak apart and strong together. A stale `summary.md` from an earlier run
would satisfy the second alone, which is why the workflow deletes the directory first; a model
reciting a total it inferred would satisfy the first alone.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The grand total over the `sales.csv` that ships beside the sample: north 390, south 200,
#: east 84, west 450. The task has exactly one right answer, so the literal is required.
_GRAND_TOTAL = "1124"

#: Every region in the file, and what the summary must report for each.
_REGION_TOTALS = {"north": "390", "south": "200", "east": "84", "west": "450"}

#: What the declared output must be called once delivered — not `<run-id>/summary.md`.
_SUMMARY_NAME = "summary.md"

_DISPOSED = re.compile(r"Disposed\s+(\d+)\s+sandbox", re.IGNORECASE)
_LANDED = re.compile(r"^Landed in .*?:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def assess(output: str, summary: str | None) -> list[str]:
    """Return every reason this was not a healthy run — empty means it passed.

    ``summary`` is the landed file's text, or ``None`` when it is not there at all. Kept as an
    argument rather than read here so the whole judgement stays a pure function.
    """
    failures: list[str] = []

    if _GRAND_TOTAL not in output:
        failures.append(
            f"{_GRAND_TOTAL!r} is not in the output — the grand total over sales.csv did not "
            "come back, so the program did not read the file it was given"
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

    landed = _LANDED.search(output)
    if landed is None:
        failures.append(
            "no 'Landed in ...' line — the sample did not reach its final report"
        )
    elif _SUMMARY_NAME not in landed.group(1):
        failures.append(
            f"the host reported landing {landed.group(1).strip()!r}, which does not include "
            f"{_SUMMARY_NAME!r} — a declared output was not delivered under its declared name"
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

    lowered = summary.lower()
    for region, total in _REGION_TOTALS.items():
        if region not in lowered:
            failures.append(f"{_SUMMARY_NAME} does not mention the {region} region")
        elif total not in summary:
            failures.append(
                f"{_SUMMARY_NAME} does not carry {region}'s total of {total}"
            )

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
