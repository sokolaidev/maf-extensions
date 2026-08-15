"""Assert that a live `samples/13_bicep_fix_loop` run actually repaired the file.

    python samples/13_bicep_fix_loop/agent.py | tee out.txt
    python scripts/check_live_fix_loop_sample.py out.txt   # or: ... | python …

The sample makes two claims and this checks both, because each is worthless alone.

**One sandbox served the whole run.** Three `acquire` calls — turn 1, turn 2, and the
independent compile — and `docker ps -a` must report exactly 1 container after each. A second
container would still answer every call, so the count is the only thing separating get-or-create
from create-every-time.

**The model repaired the file rather than describing a repair.** `main.bicep changed` comes from
comparing the file store against what went in, so it cannot be satisfied by prose. At least one
of the two faults must be gone, and `faults fixed` plus `faults remaining` must still account
for both.

Those two numbers come from a substring search over the model's file, so the last assertion
holds them to the compiler **rule by rule**: what the tally calls fixed the compiler must no
longer report, and what it calls remaining the compiler must still report. A model that deletes
the offending lines and breaks something else satisfies the tally and fails here, which is the
point of compiling again at all.

`main.bicep` also reports `use-recent-api-versions`, which fires on the age of the API version
rather than on anything structural. This sample neither asks for it nor forbids fixing it, and
comparing per rule is what keeps that out of the result.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The two structural faults in `main.bicep`, by the rule id the compiler reports for each. The
#: file is sample 05's, unedited, so these are the faults that sample already reports.
_RULE_IDS = ("no-unused-params", "BCP035")

#: Turn 1's prose, and only that. The rule ids appear again further down in the sample's own
#: bookkeeping, which the *sample* prints — searching the whole output would pass on that literal
#: whatever the model said, so the section is cut out before the ids are looked for.
_TURN_ONE = (re.compile(r"==\s*Turn 1\b"), re.compile(r"containers after turn 1"))

#: `docker ps -a` after each of the three acquires. All three must read 1.
_COUNTS = (
    ("after turn 1", re.compile(r"containers after turn 1:\s*(\d+)", re.IGNORECASE)),
    ("after turn 2", re.compile(r"containers after turn 2:\s*(\d+)", re.IGNORECASE)),
    ("after the check", re.compile(r"containers after the check:\s*(\d+)", re.IGNORECASE)),
)

#: The work product: the file store compared with what went in, and the fault tally. Both tally
#: lines name their faults after the count, and the names are what the compiler is held to below.
_CHANGED = re.compile(r"main\.bicep changed:\s*(True|False)", re.IGNORECASE)
_FIXED = re.compile(r"faults fixed:\s*(\d+)\s*[-—]\s*([^\n]*)", re.IGNORECASE)
_REMAINING = re.compile(r"faults remaining:\s*(\d+)\s*[-—]\s*([^\n]*)", re.IGNORECASE)

#: The independent compile at the end, and only that. `main.bicep` compiles in two phases and
#: each prints one line; `format_diagnostics` renders "no diagnostics" or "N diagnostic(s)".
_COMPILE = (re.compile(r"==\s*Independent check"), re.compile(r"containers after the check"))
_PHASE = re.compile(r"^\s*(build|lint)\([^)]*\):\s*(no diagnostics|\d+ diagnostic)", re.MULTILINE)

#: The footer, both numbers read back from what the run observed.
_FOOTER = re.compile(
    r"Disposed\s+(\d+)\s+sandbox\(es\)[^.]*\.\s*Containers left:\s*(\d+)\.", re.IGNORECASE
)


def _one(pattern: re.Pattern[str], output: str) -> str | None:
    match = pattern.search(output)
    return match.group(1) if match else None


def _section(output: str, bounds: tuple[re.Pattern[str], re.Pattern[str]]) -> str | None:
    """The text between two markers, or ``None`` if either is missing."""
    start, end = bounds
    opened = start.search(output)
    if opened is None:
        return None
    closed = end.search(output, opened.end())
    return output[opened.end() : closed.start()] if closed else None


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []
    failures.extend(_assess_first_turn(output))
    failures.extend(_assess_reuse(output))
    failures.extend(_assess_repair(output))
    failures.extend(_assess_footer(output))
    return failures


def _assess_first_turn(output: str) -> list[str]:
    """Turn 1 must report the compiler's real rule ids, in its own words."""
    reported = _section(output, _TURN_ONE)
    if reported is None:
        return ["no turn 1 section — the sample did not get as far as validating"]
    missing = [rule for rule in _RULE_IDS if rule.lower() not in reported.lower()]
    if missing:
        return [
            f"turn 1 did not name {', '.join(missing)} — the fix turn is asked to repair "
            "'the faults those diagnostics point at', so a first turn that did not report them "
            "leaves the second one with nothing to work from"
        ]
    return []


def _assess_reuse(output: str) -> list[str]:
    """One sandbox across three acquires — the claim the sample exists to make."""
    failures: list[str] = []
    for where, pattern in _COUNTS:
        count = _one(pattern, output)
        if count is None:
            failures.append(f"no container count {where} — reuse is unshown at that point")
        elif int(count) != 1:
            failures.append(
                f"{count} container(s) {where}, expected exactly 1 — a second sandbox answers "
                "the call just as well, so this count is what distinguishes acquire reusing one "
                "from acquire creating another"
            )
    return failures


def _assess_repair(output: str) -> list[str]:
    """The file changed, a fault went away, and the compiler agrees with the tally."""
    failures: list[str] = []

    changed = _one(_CHANGED, output)
    if changed is None:
        failures.append("the run never reported whether main.bicep changed")
    elif changed.lower() != "true":
        failures.append(
            "main.bicep is byte-identical to what went in — the model described a fix and did "
            "not make one, which is the exact failure this sample reads the file store to catch"
        )

    fixed, remaining = _FIXED.search(output), _REMAINING.search(output)
    if fixed is None or remaining is None:
        failures.append("the run never reported its fault tally")
        return failures

    fixed_count, fixed_names = int(fixed.group(1)), fixed.group(2)
    remaining_count, remaining_names = int(remaining.group(1)), remaining.group(2)

    if fixed_count < 1:
        failures.append(
            "no fault was fixed — the file may have changed, but not in a way that removed "
            "either fault the diagnostics pointed at"
        )
    if fixed_count + remaining_count != len(_RULE_IDS):
        failures.append(
            f"{fixed_count} fixed and {remaining_count} remaining do not account for the "
            f"{len(_RULE_IDS)} faults this sample tracks — the tally lost one"
        )

    failures.extend(_assess_compiler_agrees(output, fixed_names, remaining_names))
    return failures


def _assess_compiler_agrees(output: str, fixed_names: str, remaining_names: str) -> list[str]:
    """Hold the text tally to the compiler, rule by rule.

    Only the two tracked rules are compared. `main.bicep` also reports `use-recent-api-versions`,
    which fires on the *age* of the API version rather than on anything structural, so it can be
    present or gone depending on whether the model chose to bump the version — and requiring
    either answer would make this check a calendar. Comparing per rule sidesteps that entirely:
    what the tally calls fixed the compiler must no longer name, and what it calls remaining the
    compiler must still name, whatever else is in the output.
    """
    compiled = _section(output, _COMPILE)
    if compiled is None:
        return [
            "the independent compile printed nothing — without it the fault tally is a substring "
            "search over the model's own file, with no compiler behind it"
        ]
    phases = {match.group(1).lower() for match in _PHASE.finditer(compiled)}
    if phases != {"build", "lint"}:
        return [
            f"the independent compile reported {sorted(phases) or 'no'} phase(s), expected both "
            "build and lint — a file can pass one and fail the other"
        ]

    failures: list[str] = []
    for rule in _RULE_IDS:
        named = rule.lower() in compiled.lower()
        if rule.lower() in fixed_names.lower() and named:
            failures.append(
                f"the tally counts {rule} as fixed but the compiler still reports it — the edit "
                "removed the text the tally looks for without satisfying the rule"
            )
        elif rule.lower() in remaining_names.lower() and not named:
            failures.append(
                f"the tally counts {rule} as remaining but the compiler does not report it — the "
                "tally and the compiler disagree about the same file"
            )
    return failures


def _assess_footer(output: str) -> list[str]:
    footer = _FOOTER.search(output)
    if footer is None:
        return ["no footer line — the sample did not run to completion"]
    disposed, leftover = (int(group) for group in footer.groups())
    failures: list[str] = []
    if disposed != 1:
        failures.append(
            f"the router reported disposing {disposed}, expected exactly 1 — the whole run "
            "acquired one sandbox, so any other number contradicts the counts above"
        )
    if leftover != 0:
        failures.append(
            f"{leftover} container(s) left behind — this count is `docker ps -a`, so a container "
            "stopped but not removed still counts, which is what a half-finished dispose leaves"
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
        print(
            "FAIL: the fix-loop sample did not repair the file against one sandbox:",
            file=sys.stderr,
        )
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(
        "OK  two turns and a check against one sandbox, and the compiler agrees the file is fixed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
