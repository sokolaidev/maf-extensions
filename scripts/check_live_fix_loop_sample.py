"""Assert that a live `samples/13_bicep_fix_loop` run actually repaired the file.

    python samples/13_bicep_fix_loop/agent.py | tee out.txt
    python scripts/check_live_fix_loop_sample.py out.txt   # or: ... | python …

The sample makes two claims and this checks both, because each is worthless alone.

**One sandbox served the whole run.** Three `acquire` calls — turn 1, turn 2, and the compile
the program runs itself — and `docker ps -a` must report exactly 1 container after each.

The counts alone do not carry that, which is why each turn's `bicep_validate` call count is
checked first. A fix turn that edits the file and never validates it makes no second `acquire`
at all, and turn 1's container is still sitting there to be counted — so the run reads as reuse
and never exercises the claim. Both turns must show at least one call.

**The model repaired the file rather than describing a repair.** `main.bicep changed` compares
the file store against what went in, so prose cannot satisfy it. At least one tracked fault must
be gone, and `faults fixed` plus `faults remaining` must account for both.

The template also has to still be there. Deleting `main.bicep`'s contents satisfies every other
signal at once — the file changed, no tracked fault is reported, and an empty file compiles
clean — so "repaired" would be the verdict on a file with nothing left in it.

All four of those are read from the sample's closing block, never from the whole output. The
model is answering into the same stream and may write "faults fixed" in its own prose, above the
block; a `search` over everything would parse the narration instead of the computed numbers.

The tally is derived from the compiler, not from the source text, so the two must agree — a
disagreement means the halves of the output describe different files. And every diagnostic is
swept, not only the tracked ones: an edit that removes both original faults while introducing a
new `BCP0xx` names neither tracked rule, and without the sweep would pass as a clean repair.

The one exception is `use-recent-api-versions`, which fires on the age of the API version.
Demanding it be fixed would make this check a calendar; demanding it remain would forbid a
genuine repair. It is tolerated either way, and nothing else untracked is.

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
_TURN_ONE = (re.compile(r"==\s*Turn 1\b"), re.compile(r"bicep_validate calls in turn 1"))

#: `docker ps -a` after each of the three acquires. All three must read 1.
_COUNTS = (
    ("after turn 1", re.compile(r"containers after turn 1:\s*(\d+)", re.IGNORECASE)),
    ("after turn 2", re.compile(r"containers after turn 2:\s*(\d+)", re.IGNORECASE)),
    ("after the check", re.compile(r"containers after the check:\s*(\d+)", re.IGNORECASE)),
)

#: The sample's own closing block, and the only place the four lines below are read from. The
#: model is answering in the same stream and can say "faults fixed: …" or "main.bicep changed"
#: in its own prose — turn 2's reply is *above* this block, so an unscoped `search` would find
#: the narration first and parse that instead of the numbers the sample computed.
#:
#: Read with ``last=True``, which is the other half of the same problem: scoping to the heading
#: is no protection if the model prints the heading too, and the first match would then be the
#: echo. The sample's block is always the final one, because it is printed after every turn.
_WORK_PRODUCT = (re.compile(r"==\s*The work product"), None)

#: The file store compared with what went in, whether the template survived, and the fault
#: tally. Both tally lines name their faults after the count, and those names are what the
#: compiler is held to below.
_CHANGED = re.compile(r"main\.bicep changed:\s*(True|False)", re.IGNORECASE)
_INTACT = re.compile(r"storage account and output intact:\s*(True|False)", re.IGNORECASE)
_FIXED = re.compile(r"faults fixed:\s*(\d+)\s*[-—]\s*([^\n]*)", re.IGNORECASE)
_REMAINING = re.compile(r"faults remaining:\s*(\d+)\s*[-—]\s*([^\n]*)", re.IGNORECASE)

#: How many times each turn actually called the validator. The container count cannot answer
#: this: a turn that never validated leaves the previous turn's container standing, so the count
#: still reads 1 and the run looks like reuse while the second `acquire` never happened.
_TOOL_CALLS = (
    ("turn 1", re.compile(r"bicep_validate calls in turn 1:\s*(\d+)", re.IGNORECASE)),
    ("turn 2", re.compile(r"bicep_validate calls in turn 2:\s*(\d+)", re.IGNORECASE)),
)

#: The compile at the end, and only that. `main.bicep` compiles in two phases and each prints one
#: line; `format_diagnostics` renders "no diagnostics" or "N diagnostic(s)" followed by the
#: diagnostics themselves, one per line as `[level] rule @ file:line: message`.
#:
#: Also read with ``last=True``. A model describing its own validation can print this heading
#: and diagnostics under it, and taking the first match would sweep those as if the compiler
#: had reported them — failing a healthy run on rule ids the model merely quoted.
_COMPILE = (re.compile(r"==\s*What the compiler says"), re.compile(r"containers after the check"))
_PHASE = re.compile(r"^\s*(build|lint)\([^)]*\):\s*(no diagnostics|\d+ diagnostic)", re.MULTILINE)
_DIAGNOSTIC = re.compile(r"^\s*\[\w+\]\s+(\S+)\s+@", re.MULTILINE)

#: The one rule the file is allowed to still report. `use-recent-api-versions` fires on how old
#: the API version is, so demanding it be fixed would make this check a calendar and demanding it
#: remain would forbid a genuine repair. Every *other* untracked rule is a fault the model
#: introduced, and there is no reading of "the file is repaired" that survives one.
_TOLERATED_RULE = "use-recent-api-versions"

#: The footer, both numbers read back from what the run observed.
_FOOTER = re.compile(
    r"Disposed\s+(\d+)\s+sandbox\(es\)[^.]*\.\s*Containers left:\s*(\d+)\.", re.IGNORECASE
)


def _one(pattern: re.Pattern[str], output: str) -> str | None:
    match = pattern.search(output)
    return match.group(1) if match else None


def _section(
    output: str, bounds: tuple[re.Pattern[str], re.Pattern[str] | None], *, last: bool = False
) -> str | None:
    """The text between two markers, or ``None`` if a required one is missing.

    An end of ``None`` means "to the end of the output" — for the closing block, which has no
    marker after it and must still be readable when the run died before its footer.

    ``last`` picks the final start marker instead of the first, and which one a caller wants
    depends on whose text the section is meant to hold. **The model's output always comes
    first**: it is printed as each turn returns, before any block the sample writes. So for a
    block the *sample* authored, the last heading is the real one and the first may be a model
    echoing it — the failure this argument exists to prevent, because the parse then reads the
    model's own prose as the sample's findings. `_TURN_ONE` is the exception and takes the
    first: that section is deliberately the model's reply, so an echo inside it is still the
    model, which is what is being searched.
    """
    start, end = bounds
    found = list(start.finditer(output))
    if not found:
        return None
    opened = found[-1] if last else found[0]
    if end is None:
        return output[opened.end() :]
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

    # Before the counts, because they are what makes the counts mean anything. A fix turn that
    # edited the file and never validated it makes no second `acquire` at all, and turn 1's
    # container is still sitting there to be counted — so the run reads as reuse, goes green,
    # and never exercises the claim. The final compile would pass too.
    for turn, pattern in _TOOL_CALLS:
        calls = _one(pattern, output)
        if calls is None:
            failures.append(
                f"{turn} did not report how many times it called bicep_validate — without it a "
                "container count of 1 is equally consistent with that turn never acquiring"
            )
        elif int(calls) < 1:
            failures.append(
                f"{turn} never called bicep_validate — the container count after it is left "
                "over from the previous turn, so it says nothing about acquire reusing anything"
            )

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
    """The file changed, kept its point, lost a fault, and agrees with the compiler.

    Everything here is read from the sample's closing block rather than from the whole output.
    The model's own reply sits above it and is free to contain any of these phrases, and a
    `search` over the lot would find the narration first — which is the failure this sample
    exists to rule out, reintroduced in its own checker.
    """
    block = _section(output, _WORK_PRODUCT, last=True)
    if block is None:
        return ["no work-product block — the run never reported what it left behind"]

    failures: list[str] = []

    changed = _one(_CHANGED, block)
    if changed is None:
        failures.append("the run never reported whether main.bicep changed")
    elif changed.lower() != "true":
        failures.append(
            "main.bicep is byte-identical to what went in — the model described a fix and did "
            "not make one, which is the exact failure this sample reads the file store to catch"
        )

    intact = _one(_INTACT, block)
    if intact is None:
        failures.append(
            "the run never reported whether the template survived — an empty file changed, "
            "reports no tracked fault and compiles clean, so nothing else here would object"
        )
    elif intact.lower() != "true":
        failures.append(
            "the storage account or its output is gone from main.bicep — deleting the template "
            "satisfies every other signal at once, and a repair that removes the thing being "
            "repaired is not one"
        )

    fixed, remaining = _FIXED.search(block), _REMAINING.search(block)
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
    """Read the compiler's own verdict, and refuse anything it reports that is not accounted for.

    Two separate jobs. The first is consistency: the sample derives its tally from these
    diagnostics, so a tally naming a rule the compiler does not, or missing one it does, means
    the two halves of the output disagree about the same file.

    The second is the one a per-rule comparison alone would miss. Checking only the tracked
    rules passes a file whose original faults are gone and which now fails on something else
    entirely — a new `BCP0xx` names neither tracked rule, so nothing objects, and the run
    reports a repair that left the file broken. So every diagnostic is swept: a rule is
    acceptable only if the tally already calls it remaining, or it is the age rule.
    """
    compiled = _section(output, _COMPILE, last=True)
    if compiled is None:
        return [
            "the compiler was never run over the file the model left — every claim below it is "
            "then the model's own account of its work, which is what this sample exists to avoid"
        ]
    phases = {match.group(1).lower() for match in _PHASE.finditer(compiled)}
    if phases != {"build", "lint"}:
        return [
            f"the compile reported {sorted(phases) or 'no'} phase(s), expected both build and "
            "lint — a file can pass one and fail the other"
        ]

    failures: list[str] = []
    reported = {match.group(1) for match in _DIAGNOSTIC.finditer(compiled)}

    for rule in _RULE_IDS:
        named = any(rule.lower() == seen.lower() for seen in reported)
        if rule.lower() in fixed_names.lower() and named:
            failures.append(
                f"the tally counts {rule} as fixed but the compiler still reports it — the two "
                "halves of the output disagree about the same file"
            )
        elif rule.lower() in remaining_names.lower() and not named:
            failures.append(
                f"the tally counts {rule} as remaining but the compiler does not report it — the "
                "two halves of the output disagree about the same file"
            )

    accounted = {rule.lower() for rule in _RULE_IDS if rule.lower() in remaining_names.lower()}
    accounted.add(_TOLERATED_RULE.lower())
    introduced = sorted(rule for rule in reported if rule.lower() not in accounted)
    if introduced:
        failures.append(
            f"the compiler reports {', '.join(introduced)}, which the run does not account for — "
            "the tracked faults may well be gone, but the model broke something else on the way "
            f"and only {_TOLERATED_RULE} is tolerated here"
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
