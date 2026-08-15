"""Assert that a live `samples/13_bicep_fix_loop` run did what the sample claims.

    python samples/13_bicep_fix_loop/agent.py | tee out.txt
    python scripts/check_live_fix_loop_sample.py out.txt   # or: ... | python …

Three claims, checked together because each is weak alone: turn 1 authored a file with a real
fault in it, one container served all four `acquire` calls, and the compiler agrees with the
repair the run reported. The sample's README says why each is measured the way it is.

Everything is read from the blocks the sample prints and never from the model's replies around
them. A run graded on its own narration is the failure the sample exists to rule out, and this
is the easiest place to reintroduce it.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The two faults the sample's brief implies, by the rule id the compiler reports for each.
#: Samples 01, 02, 05 and 09 report the same pair from the `main.bicep` they check in.
_RULE_IDS = ("no-unused-params", "BCP035")

#: Turn 1's prose, and only that. The rule ids appear again further down in the sample's own
#: bookkeeping, which the *sample* prints — searching the whole output would pass on that literal
#: whatever the model said, so the section is cut out before the ids are looked for.
_TURN_ONE = (re.compile(r"==\s*Turn 1\b"), re.compile(r"bicep_validate calls in turn 1"))

#: `docker ps -a` after each of the four acquires. All four must read 1.
_COUNTS = (
    ("after turn 1", re.compile(r"containers after turn 1:\s*(\d+)", re.IGNORECASE)),
    (
        "after the baseline compile",
        re.compile(r"containers after the baseline compile:\s*(\d+)", re.IGNORECASE),
    ),
    ("after turn 2", re.compile(r"containers after turn 2:\s*(\d+)", re.IGNORECASE)),
    ("after the check", re.compile(r"containers after the check:\s*(\d+)", re.IGNORECASE)),
)

#: The sample's closing block, and the only place the four lines below are read from: the model
#: answers into the same stream and is free to print any of those phrases, heading included.
#: Hence ``last=True`` as well as the scope.
_WORK_PRODUCT = (re.compile(r"==\s*The work product"), None)

#: The file store compared with what went in, whether the template survived, and the fault
#: tally. Both tally lines name their faults after the count, and those names are what the
#: compiler is held to below.
_AUTHORED = re.compile(r"main\.bicep authored in turn 1:\s*(True|False)", re.IGNORECASE)
_CHANGED = re.compile(r"main\.bicep changed by turn 2:\s*(True|False)", re.IGNORECASE)
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

#: The program's own compile of the file turn 1 wrote — the baseline the repair is measured
#: against, and it must correspond to that snapshot rather than to anything the model quoted.
#: Ends at the container count so the fault tally below is inside the section and read from it.
_AUTHORED_COMPILE = (
    re.compile(r"==\s*What the compiler says about the file turn 1 wrote"),
    re.compile(r"containers after the baseline compile"),
)
_AUTHORED_FAULTS = re.compile(
    r"tracked faults in the authored file:\s*(\d+)\s*[-—]\s*([^\n]*)", re.IGNORECASE
)

#: The program's compile of the file turn 2 left. Matched on the full heading, because the
#: baseline block opens with the same five words. Each phase prints one line, then its
#: diagnostics as `[level] rule @ file:line: message`.
_COMPILE = (
    re.compile(r"==\s*What the compiler says about the file the model left"),
    re.compile(r"containers after the check"),
)
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


def _tally(match: re.Match[str], label: str) -> tuple[set[str], list[str]]:
    """The rule ids on a tally line, held to the number printed beside them.

    A count and a list that disagree describe different things, and a name outside `_RULE_IDS`
    is not something this checker can hold the compiler to.
    """
    count = int(match.group(1))
    body = match.group(2).strip()
    names = [] if body.lower() == "none" else [part.strip() for part in body.split(";")]
    names = [name for name in names if name]

    failures: list[str] = []
    if len(names) != count:
        failures.append(
            f"`faults {label}` says {count} but names {len(names)}: {body!r} — the number and "
            "the list beside it describe different things"
        )
    unknown = sorted(name for name in names if name not in _RULE_IDS)
    if unknown:
        failures.append(
            f"`faults {label}` names {', '.join(unknown)}, which this sample does not track — "
            f"the only rule ids it reports on are {', '.join(_RULE_IDS)}"
        )
    return set(names), failures


def _section(
    output: str, bounds: tuple[re.Pattern[str], re.Pattern[str] | None], *, last: bool = False
) -> str | None:
    """The text between two markers, or ``None`` if a required one is missing.

    An end of ``None`` means "to the end of the output".

    ``last`` selects the final start marker **its end marker still follows**, for blocks the
    sample writes and a model could quote. Simply taking the final match is not enough: the
    baseline block is printed between the two turns, so a turn-2 reply repeating its heading
    comes after it, and the end marker is then already behind the match — which reads as a
    missing block and fails a healthy run. `_TURN_ONE` takes the first, because that section is
    meant to hold the model's reply.
    """
    start, end = bounds
    found = list(start.finditer(output))
    for opened in reversed(found if last else found[:1]):
        if end is None:
            return output[opened.end() :]
        closed = end.search(output, opened.end())
        if closed:
            return output[opened.end() : closed.start()]
    return None


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
    """One sandbox across all four acquires — the claim the sample exists to make."""
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

    authored = _one(_AUTHORED, block)
    if authored is None:
        failures.append("the run never reported whether turn 1 wrote main.bicep")
    elif authored.lower() != "true":
        failures.append(
            "turn 1 left no main.bicep in the store — the store starts empty in this sample, so "
            "the authoring half of author → validate → fix did not happen and everything after "
            "it is about a file that was never written"
        )

    changed = _one(_CHANGED, block)
    if changed is None:
        failures.append("the run never reported whether turn 2 changed main.bicep")
    elif changed.lower() != "true":
        failures.append(
            "main.bicep is byte-identical to what turn 1 wrote — the fix turn described a repair "
            "and did not make one, which is the exact failure this sample reads the store to "
            "catch"
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

    fixed_names, fixed_failures = _tally(fixed, "fixed")
    remaining_names, remaining_failures = _tally(remaining, "remaining")
    failures.extend(fixed_failures)
    failures.extend(remaining_failures)

    both = fixed_names & remaining_names
    if both:
        failures.append(
            f"{', '.join(sorted(both))} is listed as both fixed and remaining — one file cannot "
            "have a rule in both states"
        )

    if len(fixed_names) < 1:
        failures.append(
            "no fault was fixed — the file may have changed, but not in a way that removed any "
            "fault the diagnostics pointed at"
        )

    # Against what turn 1 actually produced, not a constant. The brief is written so a compliant
    # model writes both faults, but the file is the model's, so the baseline has to be measured.
    failures.extend(
        _assess_against_the_authored_file(output, len(fixed_names), len(remaining_names))
    )
    failures.extend(_assess_compiler_agrees(output, fixed_names, remaining_names))
    return failures


def _assess_against_the_authored_file(output: str, fixed: int, remaining: int) -> list[str]:
    """The baseline: what the compiler said about the file turn 1 wrote.

    Two things depend on it. The tally has to add up against *that* number rather than against a
    constant — the file is the model's, so how many tracked faults it arrived with is a
    measurement. And the count has to be at least one, because a run whose authored file was
    already clean has nothing for turn 2 to repair: it would sail through every other assertion
    here while demonstrating no fix loop at all, which is the whole subject of the sample.
    """
    compiled = _section(output, _AUTHORED_COMPILE, last=True)
    if compiled is None:
        return [
            "the run never showed what the compiler said about the file turn 1 wrote — without "
            "that baseline there is nothing to measure the repair against"
        ]

    # Both phases, exactly as the final compile demands. Accepting one would let the baseline be
    # counted from a partial compile: a lint-only failure would go unseen, the tracked-fault
    # count would come out short, and turn 2 would be measured against the wrong starting point.
    phases = {match.group(1).lower() for match in _PHASE.finditer(compiled)}
    if phases != {"build", "lint"}:
        return [
            f"the baseline compile reported {sorted(phases) or 'no'} phase(s), expected both "
            "build and lint — a partial compile undercounts what the authored file started with"
        ]

    match = _AUTHORED_FAULTS.search(compiled)
    if match is None:
        return ["the run never counted the tracked faults in the authored file"]

    count = int(match.group(1))
    if count < 1:
        return [
            "the file turn 1 wrote had no tracked fault in it — the brief asks for an unused "
            "parameter and no sku, so a clean file means the model did not follow it, and there "
            "was nothing for the fix turn to repair"
        ]
    if fixed + remaining != count:
        return [
            f"{fixed} fixed and {remaining} remaining do not account for the {count} the "
            "authored file had — the tally lost one, or gained one turn 2 introduced"
        ]
    return []


def _assess_compiler_agrees(
    output: str, fixed_names: set[str], remaining_names: set[str]
) -> list[str]:
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
        if rule in fixed_names and named:
            failures.append(
                f"the tally counts {rule} as fixed but the compiler still reports it — the two "
                "halves of the output disagree about the same file"
            )
        elif rule in remaining_names and not named:
            failures.append(
                f"the tally counts {rule} as remaining but the compiler does not report it — the "
                "two halves of the output disagree about the same file"
            )

    accounted = {rule.lower() for rule in remaining_names}
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
    # "agrees with the repair reported", not "the file is fixed". A run that fixed one of two
    # faults and says so is accepted here on purpose, and the old wording claimed a clean file
    # over one the compiler still reports an error on.
    print(
        "OK  the model wrote main.bicep and repaired it against one sandbox, "
        "and the compiler agrees with the repair the run reported"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
