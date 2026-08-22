"""Assert that a live `samples/13_bicep_fix_loop` run did what the sample claims.

    python samples/13_bicep_fix_loop/agent.py | tee out.txt
    python scripts/check_live_fix_loop_sample.py out.txt   # or: ... | python …

Three claims, checked together because each is weak alone: turn 1 authored a file with a real
fault in it, one container served all four `acquire` calls, and the compiler agrees with the
repair the run reported. The sample's README says why each is measured the way it is.

Every *number* comes off a line the sample tagged `[measured]`, never from the model's replies
around it. The one thing read out of a reply is turn 1's prose, which has to name the rules the
compiler found in the file turn 1 wrote — that is the claim being checked, so the model's own
words are the subject rather than the evidence.

Exits non-zero listing every reason it failed, and says **which half** failed in the
status: 3 when every failure was the model's own — either turn — and 1 when anything this
suite owns was wrong too. `verify-live.yml` retries the loop on 3 and on nothing else, up to a
budget it owns and this file does not name (#421).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Every number below is read only off a line the *sample* tagged. The model answers into the
#: same stream, so an unmarked search finds a reply quoting "containers after turn 2: 2" before
#: the sample's own count — and that fails a healthy run. `MEASURED` in the sample writes the
#: tag; nothing else in the output carries it.
#:
#: The tag matches case-sensitively — `(?-i:…)` — while the phrase after it keeps `_F`'s
#: `IGNORECASE`. The sample emits one fixed spelling, so accepting others only widens what has
#: to be sanitized, and a reader broader than its sanitizer is a hole rather than tolerance.
_M = r"^  (?-i:\[measured\]) "
_F = re.MULTILINE | re.IGNORECASE

#: The two faults the sample's brief implies, by the rule id the compiler reports for each.
#: Samples 01, 02, 05 and 09 report the same pair from the `main.bicep` they check in.
_RULE_IDS = ("no-unused-params", "BCP035")

#: Where each tracked rule's diagnostic names what it is *about*. The sample declares the same
#: pair; it is repeated here so a fault's identity is derived from the compiler's own text
#: rather than taken from the tally being checked.
#:
#: A rule id alone cannot tell two instances apart, and that is not hypothetical: a turn that
#: added the missing `sku` and left `location` missing reported `BCP035` before and after, so
#: the tally read 0 fixed and the failure said no fault had been removed (#432).
_FAULT_TARGETS = {
    "no-unused-params": re.compile(r'\bparameter\s+("[^"]+")', re.IGNORECASE),
    "BCP035": re.compile(r"required propert(?:y|ies):\s*([^.]+)", re.IGNORECASE),
}

#: Turn 1's prose, and only that. The rule ids appear again further down in the sample's own
#: bookkeeping, which the *sample* prints — searching the whole output would pass on that literal
#: whatever the model said, so the section is cut out before the ids are looked for.
_TURN_ONE = (
    re.compile(r"==\s*Turn 1\b"),
    re.compile(_M + r"validations that reached the sandbox in turn 1", _F),
)

#: `docker ps -a` after each of the four acquires: the count, then the ids behind it. All four
#: must read 1, and all four must name the *same* id — one container existing at four instants
#: is not one container serving all four, and a backend that force-removes on a timeout would
#: satisfy the count while the second acquire paid for a fresh create.
_COUNTS = (
    ("after turn 1", re.compile(_M + r"containers after turn 1:\s*(\d+)\s*\(([^)]*)\)", _F)),
    (
        "after the baseline compile",
        re.compile(_M + r"containers after the baseline compile:\s*(\d+)\s*\(([^)]*)\)", _F),
    ),
    ("after turn 2", re.compile(_M + r"containers after turn 2:\s*(\d+)\s*\(([^)]*)\)", _F)),
    ("after the check", re.compile(_M + r"containers after the check:\s*(\d+)\s*\(([^)]*)\)", _F)),
)

#: The sample's closing block, and the only place the four lines below are read from: the model
#: answers into the same stream and is free to print any of those phrases, heading included.
#: Hence ``last=True`` as well as the scope.
_WORK_PRODUCT = (re.compile(r"==\s*The work product"), None)

#: The file store compared with what went in, whether the template survived, and the fault
#: tally. Both tally lines name their faults after the count, and those names are what the
#: compiler is held to below.
_AUTHORED = re.compile(_M + r"main\.bicep authored in turn 1:\s*(True|False)", _F)
_CHANGED = re.compile(_M + r"main\.bicep changed by turn 2:\s*(True|False)", _F)
_INTACT = re.compile(_M + r"storage account and output intact:\s*(True|False)", _F)
_SUPPRESSED = re.compile(_M + r"tracked rules suppressed:\s*(\d+)\s*[-—]\s*([^\n]*)", _F)
_FIXED = re.compile(_M + r"faults fixed:\s*(\d+)\s*[-—]\s*([^\n]*)", _F)
_REMAINING = re.compile(_M + r"faults remaining:\s*(\d+)\s*[-—]\s*([^\n]*)", _F)
_INTRODUCED = re.compile(_M + r"faults introduced:\s*(\d+)\s*[-—]\s*([^\n]*)", _F)

#: How many times each turn reached the sandbox. The container count cannot answer this: a turn
#: that never validated leaves the previous turn's container standing, so the count still reads
#: 1 while no second `acquire` happened. Counted from results rather than requests, because the
#: tool refuses some calls before it acquires anything.
_TOOL_CALLS = (
    ("turn 1", re.compile(_M + r"validations that reached the sandbox in turn 1:\s*(\d+)", _F)),
    ("turn 2", re.compile(_M + r"validations that reached the sandbox in turn 2:\s*(\d+)", _F)),
)

#: The program's own compile of the file turn 1 wrote — the baseline the repair is measured
#: against, and it must correspond to that snapshot rather than to anything the model quoted.
#: Ends at the container count so the fault tally below is inside the section and read from it.
_AUTHORED_COMPILE = (
    re.compile(r"==\s*What the compiler says about the file turn 1 wrote"),
    re.compile(_M + r"containers after the baseline compile", _F),
)
_AUTHORED_FAULTS = re.compile(
    _M + r"tracked faults in the authored file:\s*(\d+)\s*[-—]\s*([^\n]*)", _F
)

#: The program's compile of the file turn 2 left. Matched on the full heading, because the
#: baseline block opens with the same five words. Each phase prints one line, then its
#: diagnostics as `[level] rule @ file:line: message`.
_COMPILE = (
    re.compile(r"==\s*What the compiler says about the file the model left"),
    re.compile(_M + r"containers after the check", _F),
)
_PHASE = re.compile(r"^\s*(build|lint)\([^)]*\):\s*(no diagnostics|\d+ diagnostic)", re.MULTILINE)
_DIAGNOSTIC = re.compile(r"^[ \t]*\[(\w+)\][ \t]+(\S+)[ \t]+@[ \t]*(.*)$", re.MULTILINE)

#: The one rule the file is allowed to still report. `use-recent-api-versions` fires on how old
#: the API version is, so demanding it be fixed would make this check a calendar and demanding it
#: remain would forbid a genuine repair. Every *other* untracked rule is a fault the model
#: introduced, and there is no reading of "the file is repaired" that survives one.
_TOLERATED_RULE = "use-recent-api-versions"

#: The footer, both numbers read back from what the run observed.
_FOOTER = re.compile(
    _M + r"Disposed\s+(\d+)\s+sandbox\(es\)[^.]*\.\s*Containers left:\s*(\d+)\.", _F
)


def _named(rule: str, tail: str) -> str:
    """A fault as ``rule(target)`` — the rule alone where the diagnostic names no target."""
    pattern = _FAULT_TARGETS.get(rule)
    found = pattern.search(tail) if pattern else None
    targets = sorted(set(re.findall(r'"([^"]+)"', found.group(1)))) if found else []
    return f"{rule}({', '.join(targets)})" if targets else rule


def _rule_of(fault: str) -> str:
    """The rule id a tally entry names, without its target."""
    return fault.split("(", 1)[0]


def _tracked(compiled: str) -> set[str]:
    """Every tracked fault a compile block reports, by identity."""
    return {
        _named(rule, diagnostic.group(3))
        for diagnostic in _DIAGNOSTIC.finditer(compiled)
        for rule in _RULE_IDS
        if diagnostic.group(2).lower() == rule.lower()
    }


class _TheModelsHalf(str):
    """Mark model-owned failures while retaining string behavior for existing callers."""


#: What `main` exits when every failure was the model's own, in either turn. `verify-live.yml`
#: runs the loop once more on this and on nothing else (#421); 1 stays what it always was —
#: something this suite owns is wrong, and another model attempt cannot mend it. A workflow that
#: predates this sees a non-zero exit and fails, which is what it did before.
MODEL_DID_NOT_CONVERGE = 3


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
    duplicated = sorted({name for name in names if names.count(name) > 1})
    if duplicated:
        failures.append(
            f"`faults {label}` names {', '.join(duplicated)} more than once — the count is then "
            "larger than the set of rules it describes"
        )
    unknown = sorted(name for name in names if _rule_of(name) not in _RULE_IDS)
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
    authored, failures = _authored_faults(output)
    failures.extend(_assess_first_turn(output, authored))
    failures.extend(_assess_reuse(output))
    failures.extend(_assess_repair(output, authored))
    failures.extend(_assess_footer(output))
    return failures


def _authored_faults(output: str) -> tuple[set[str], list[str]]:
    """Which tracked rules the compiler reported in the file turn 1 wrote.

    Everything downstream is measured against this rather than against `_RULE_IDS`. The file is
    the model's, so which faults it arrives with is an observation: a brief that asks for a
    parameter "a later change will use" is satisfied by a model that uses it immediately, and
    that file has one tracked fault, not two. At least one is required — a clean authored file
    leaves the fix turn nothing to do.
    """
    compiled = _section(output, _AUTHORED_COMPILE, last=True)
    if compiled is None:
        return set(), [
            "the run never showed what the compiler said about the file turn 1 wrote — without "
            "that baseline there is nothing to measure the repair against"
        ]

    # Both phases, exactly as the final compile demands. A partial compile would undercount what
    # the authored file started with, and turn 2 would be measured from the wrong place.
    phases = {match.group(1).lower() for match in _PHASE.finditer(compiled)}
    if phases != {"build", "lint"}:
        return set(), [
            f"the baseline compile reported {sorted(phases) or 'no'} phase(s), expected both "
            "build and lint — a partial compile undercounts what the authored file started with"
        ]

    match = _AUTHORED_FAULTS.search(compiled)
    if match is None:
        return set(), ["the run never counted the tracked faults in the authored file"]

    names, failures = _tally(match, "in the authored file")

    # Against the diagnostics printed directly above it, as the final compile is. Without this
    # the baseline is a number with nothing behind it: drop a diagnostic line from the block and
    # every later comparison is measured from a starting point the compiler never reported.
    reported = _tracked(compiled)
    if names != reported:
        failures.append(
            f"the baseline names {sorted(names) or 'nothing'} but its own compile reports "
            f"{sorted(reported) or 'nothing'} — the tally and the diagnostics above it "
            "describe different files"
        )

    if not names and not failures:
        failures.append(
            _TheModelsHalf(
                "the file turn 1 wrote had no tracked fault in it — the brief asks for an unused "
                "parameter and no sku, so a clean file means the model did not follow it, and there "
                "was nothing for the fix turn to repair"
            )
        )
    return names, failures


def _assess_first_turn(output: str, authored: set[str]) -> list[str]:
    """Turn 1 must report, in its own words, the tracked rules its file actually has.

    Against `authored` and not `_RULE_IDS`: demanding both would fail a run whose authored file
    only ever had one, which is a fix loop the sample is happy to demonstrate.
    """
    reported = _section(output, _TURN_ONE)
    if reported is None:
        return ["no turn 1 section — the sample did not get as far as validating"]
    missing = sorted(
        {_rule_of(fault) for fault in authored if _rule_of(fault).lower() not in reported.lower()}
    )
    if missing:
        return [
            _TheModelsHalf(
                f"turn 1 did not name {', '.join(missing)} — the compiler reported it on the file "
                "turn 1 wrote, and the fix turn is asked to repair 'the faults those diagnostics "
                "point at', so a first turn that did not report it leaves the second one short"
            )
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
                f"{turn} did not report how many validations reached the sandbox — without it a "
                "container count of 1 is equally consistent with that turn never acquiring"
            )
        elif int(calls) < 1:
            failures.append(
                f"{turn} reached the sandbox no times — the container count after it is left "
                "over from the previous turn, so it says nothing about acquire reusing anything"
            )

    seen: dict[str, str] = {}
    for where, pattern in _COUNTS:
        match = pattern.search(output)
        if match is None:
            failures.append(f"no container count {where} — reuse is unshown at that point")
            continue
        count, ids = int(match.group(1)), match.group(2).strip()
        if count != 1:
            failures.append(
                f"{count} container(s) {where}, expected exactly 1 — a second sandbox answers "
                "the call just as well, so this count is what distinguishes acquire reusing one "
                "from acquire creating another"
            )
        else:
            seen[where] = ids

    # The same one, not merely one. Without this the sample measures existence at four instants
    # and claims continuity across them, which is a different sentence.
    distinct = set(seen.values())
    if len(distinct) > 1:
        failures.append(
            f"the container changed during the run: {', '.join(f'{k} {v}' for k, v in seen.items())}"
            " — one sandbox existed at each point, but not the same one, so an acquire created "
            "a replacement rather than finding what was there"
        )
    return failures


def _assess_repair(output: str, authored: set[str]) -> list[str]:
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

    wrote_file = _one(_AUTHORED, block)
    if wrote_file is None:
        failures.append("the run never reported whether turn 1 wrote main.bicep")
    elif wrote_file.lower() != "true":
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
            _TheModelsHalf(
                "the storage account or its output is gone from main.bicep — deleting the template "
                "satisfies every other signal at once, and a repair that removes the thing being "
                "repaired is not one"
            )
        )

    silenced = _one(_SUPPRESSED, block)
    if silenced is None:
        failures.append(
            "the run never reported whether a tracked rule was suppressed — a "
            "`#disable-next-line` makes the compiler stop reporting it, and every other signal "
            "here reads that as repaired"
        )
    elif int(silenced) != 0:
        failures.append(
            f"{silenced} tracked rule(s) silenced with `#disable-next-line` — the diagnostic is "
            "gone because the file told the compiler not to look, which is not the repair turn "
            "2 was asked for"
        )

    fixed = _FIXED.search(block)
    remaining, introduced = _REMAINING.search(block), _INTRODUCED.search(block)
    if fixed is None or remaining is None or introduced is None:
        failures.append("the run never reported its fault tally")
        return failures

    fixed_names, fixed_failures = _tally(fixed, "fixed")
    remaining_names, remaining_failures = _tally(remaining, "remaining")
    introduced_names, introduced_failures = _tally(introduced, "introduced")
    failures.extend(fixed_failures)
    failures.extend(remaining_failures)
    failures.extend(introduced_failures)

    for label, both in (
        ("fixed and remaining", fixed_names & remaining_names),
        ("fixed and introduced", fixed_names & introduced_names),
        ("remaining and introduced", remaining_names & introduced_names),
    ):
        if both:
            failures.append(
                f"{', '.join(sorted(both))} is listed as both {label} — the three lists divide "
                "what the two compiles reported, so nothing belongs to two of them"
            )

    # What turn 2 did, in the three shapes it can take. One sentence used to cover all of them
    # and was false for the middle one: a run that added the missing `sku` and left `location`
    # missing was told no fault the diagnostics pointed at had been removed (#432).
    if not fixed_names and not introduced_names:
        failures.append(
            _TheModelsHalf(
                "no fault was fixed — the file may have changed, but every fault the diagnostics "
                "pointed at is still there, on the same target"
            )
        )
    elif not fixed_names:
        failures.append(
            _TheModelsHalf(
                f"no fault was fixed, and the file now reports {', '.join(sorted(introduced_names))} "
                "as well — turn 2 edited the file into a worse one"
            )
        )
    elif introduced_names:
        failures.append(
            _TheModelsHalf(
                f"the repair traded one diagnostic for another: {', '.join(sorted(fixed_names))} "
                f"went and {', '.join(sorted(introduced_names))} arrived. Turn 2 is asked to leave "
                "the file reporting nothing it did not report before, and partial progress is still "
                "a loop that did not converge"
            )
        )

    # Against what turn 1 actually produced, not a constant. `introduced` is deliberately not in
    # this sum: it describes the file turn 2 left, and this asks about the one turn 1 wrote.
    if authored and fixed_names | remaining_names != authored:
        failures.append(
            f"the tally covers {sorted(fixed_names | remaining_names) or 'nothing'} but the "
            f"authored file had {sorted(authored)} — the two do not describe the same file"
        )
    failures.extend(_assess_compiler_agrees(output, fixed_names, remaining_names, introduced_names))
    return failures


def _baseline_warnings(output: str) -> dict[str, int]:
    """How many times the file turn 1 wrote drew each **non-error** rule.

    Warnings only. Turn 2 is asked to leave the file reporting nothing it did not report
    before, and an authoring tic like `simplify-interpolation` is not this turn's doing — but a
    file that does not *build* was never repaired, whoever introduced the error. Tolerating
    baseline errors would pass a final compile that still fails, which is more than that
    promise needs.

    Counted rather than collected, so one baseline warning licenses one, not any number.
    """
    compiled = _section(output, _AUTHORED_COMPILE, last=True)
    if compiled is None:
        return {}
    counts: dict[str, int] = {}
    for diagnostic in _DIAGNOSTIC.finditer(compiled):
        level, rule = diagnostic.group(1).lower(), diagnostic.group(2)
        if level != "error":
            counts[rule] = counts.get(rule, 0) + 1
    return counts


def _assess_compiler_agrees(
    output: str, fixed_names: set[str], remaining_names: set[str], introduced_names: set[str]
) -> list[str]:
    """Read the compiler's own verdict, and refuse anything it reports that is not accounted for.

    Two separate jobs. The first is consistency: the sample derives its tally from these
    diagnostics, so a tally naming a fault the compiler does not, or missing one it does, means
    the two halves of the output disagree about the same file. Tracked faults are compared by
    identity, which is what makes a swap within one rule visible here rather than invisible.

    The second is the one a tracked-rule comparison alone would miss. Checking only the two
    rules passes a file whose original faults are gone and which now fails on something else
    entirely — a new `BCP0xx` names neither, so nothing objects, and the run reports a repair
    that left the file broken. So every other diagnostic is swept too: an untracked rule is
    acceptable only if the authored file already drew it as often, or it is the age rule.
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

    # The tracked faults, by `rule(target)`. What the compiler still reports is exactly what
    # the tally calls remaining plus what it calls introduced; anything else is a disagreement.
    reported = _tracked(compiled)
    for fault in sorted(fixed_names & reported):
        failures.append(
            f"the tally counts {fault} as fixed but the compiler still reports it — the two "
            "halves of the output disagree about the same file"
        )
    for label, claimed in (("remaining", remaining_names), ("introduced", introduced_names)):
        for fault in sorted(claimed - reported):
            failures.append(
                f"the tally counts {fault} as {label} but the compiler does not report it — the "
                "two halves of the output disagree about the same file"
            )
    for fault in sorted(reported - remaining_names - introduced_names - fixed_names):
        failures.append(
            f"the compiler reports {fault} and the tally counts it neither remaining nor "
            "introduced — a tracked fault the run does not account for at all"
        )

    # Everything else the compiler said. Tracked rules are held to the tally above; these have
    # no target to compare, so the question is whether the authored file already drew them.
    #
    # "Introduced" means the baseline did not report it that often. Turn 2 is asked to leave the
    # file reporting nothing it did not report before, so a rule the authored file already had
    # is not this turn's doing — an ordinary authoring tic like `simplify-interpolation` would
    # otherwise fail a run the prompt explicitly licensed, blaming the model for it. Counted
    # rather than collected, so one baseline warning licenses one instance, not any number.
    baseline = _baseline_warnings(output)
    final: dict[str, int] = {}
    for diagnostic in _DIAGNOSTIC.finditer(compiled):
        rule = diagnostic.group(2)
        if rule.lower() not in {tracked.lower() for tracked in _RULE_IDS}:
            final[rule] = final.get(rule, 0) + 1

    introduced = sorted(
        rule
        for rule, seen in final.items()
        if rule.lower() != _TOLERATED_RULE.lower() and seen > baseline.get(rule, 0)
    )
    if introduced:
        failures.append(
            _TheModelsHalf(
                f"the compiler reports {', '.join(introduced)}, which the run does not account for — "
                "the tracked faults may well be gone, but the model broke something else on the way "
                f"and only {_TOLERATED_RULE} is tolerated here"
            )
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
        # Which half failed, said out loud rather than left to the exit status. A reader of the
        # log is the first consumer; `verify-live.yml` is the second (#421).
        if all(isinstance(reason, _TheModelsHalf) for reason in failures):
            print(
                "  every failure above is the model's own — the sandbox was reused, both "
                "turns reached it, the file was written and changed, and nothing was left "
                f"behind. Exiting {MODEL_DID_NOT_CONVERGE}: the loop is worth another attempt.",
                file=sys.stderr,
            )
            return MODEL_DID_NOT_CONVERGE
        return 1
    # "agrees with the repair reported", not "the file is fixed": a run that repaired one of two
    # faults and said so passes, and the compiler still reports an error on it.
    print(
        "OK  the model wrote main.bicep and repaired it against one sandbox, "
        "and the compiler agrees with the repair the run reported"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
