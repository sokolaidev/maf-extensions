"""Assert that a live Bicep-sample run actually validated the file.

The live workflow installs the *published* wheels, runs one of the Bicep samples —
`samples/01_acas_bicep` against Azure, `samples/05_docker_bicep` in a local container,
`samples/09_inprocess_bicep` in-process — and pipes its output here. They all compile the
same `main.bicep` with the same CLI, so the same two rule ids come back whatever ran them;
one assertion serves every Bicep sample. This script is that assertion: it proves the
compiler's diagnostics survived the round trip through router, backend, image and workload
— the whole point of the run — without pinning anything that drifts on its own.

    python samples/01_acas_bicep/agent.py | tee out.txt
    python scripts/check_live_sample.py out.txt   # or: ... | python scripts/check_live_sample.py

The match is deliberately loose. Diagnostics carry a day count and an API-version list that
climb with no code change (see the sample README), so a whole-string comparison would become
the very drift it is meant to catch. Rule ids and severities are matched instead — the rule
ids are opaque tokens the model is instructed to echo verbatim, so their presence is evidence
the compiler ran and its findings reached the end, not that the model agreed with itself.

It also checks the compiler found `bicepconfig.json`, because nothing else can. Sample 01
boots a *disk image*, which is a snapshot: overwriting the registry tag it was imported from
leaves the booted image untouched, so an image carrying the config at a work-dir root the
tool no longer writes to keeps running — and lints against the CLI's built-in defaults
instead of the rule set this repository asked for. SARIF still parses, diagnostics still
render, and both rule ids above still come back, so every other assertion here survives it.
See #308.

Exits non-zero listing every reason it failed. A run that never created a sandbox
(`Disposed 0`) or never produced these diagnostics is a broken stack, not a clean file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Two findings the sample's `main.bicep` always produces: `no-unused-params` (a lint finding)
#: and `BCP035` (a build finding, the missing `sku`). Their rule ids are opaque tokens the
#: model is told to echo verbatim, so requiring both is strong evidence the compiler ran and
#: its findings reached the end. Neither says anything about *which* rule set ran, which is
#: what the config check below is for.
_REQUIRED_RULES = ("no-unused-params", "BCP035")

#: A rule `bicepconfig.json` switches on. It is off by default, so it cannot appear unless the
#: config was found. Its *message* drifts — the day count climbs and the acceptable-version
#: list moves — but the rule id does not, and the direction of that drift is safe: the pinned
#: `2023-01-01` only ever gets further past the 730-day threshold, so the rule fires more over
#: time, never less.
_CONFIG_ONLY_RULE = "use-recent-api-versions"

#: `no-unused-params` and a rendered `[error]` on one line. The config promotes that rule from
#: its built-in `warning`, so the pairing cannot occur without one.
#:
#: Two lookaheads rather than a fixed order, and line-scoped rather than adjacent, because the
#: model is between the compiler and this script. The workload renders each diagnostic as
#: `  [<level>] <rule> @ <file>:<line>: <message>` (`maf_sandbox_bicep._sarif`), and the sample
#: instructs the model to report it back verbatim — but a model asked to "list every
#: diagnostic" may lay the same fields out as a table row instead. Either shape keeps a
#: diagnostic on one line, and `.` does not cross a newline, so the line is the unit.
#:
#: The brackets are load-bearing. A bare `error` also matches "no-unused-params is not an
#: error", which is what a run *without* the config would truthfully say about it.
_PROMOTED_LINT_ERROR = re.compile(
    r"^(?=.*no-unused-params)(?=.*\[error\]).*$", re.MULTILINE | re.IGNORECASE
)

_DISPOSED = re.compile(r"Disposed\s+(\d+)\s+sandbox", re.IGNORECASE)


def config_was_discovered(output: str) -> bool:
    """Whether the diagnostics show the compiler found `bicepconfig.json`.

    Two independent tells, and *either* is enough. They are not two facts — they are one fact
    seen twice, and a run that found no config loses both at the same moment: the rule the
    config switches on disappears, and the rule it promotes drops back to `warning`. So
    requiring one of the two catches the failure just as surely as requiring both, while
    staying standing if the model drops a rule from its summary or lays the severity out
    somewhere this cannot read it. Requiring both would trade a real red for a false one.

    It also leaves `bicepconfig.json` editable. Turning one of these two rules off is a
    legitimate change to the rule set; turning both off would be a decision to stop asserting
    on it here, and this should be updated with it.
    """
    return _CONFIG_ONLY_RULE in output or _PROMOTED_LINT_ERROR.search(output) is not None


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    for rule in _REQUIRED_RULES:
        if rule not in output:
            failures.append(
                f"diagnostic {rule!r} is not in the output — the compiler's findings did not come back"
            )

    # The rule set the repository asked for actually ran. Everything above passes against the
    # CLI's built-in defaults, so without this a sample can lint against a weaker rule set than
    # it claims and report success — which is exactly what a stale disk image does (#308).
    if not config_was_discovered(output):
        failures.append(
            f"neither {_CONFIG_ONLY_RULE!r} nor an [error] level on no-unused-params is in the "
            "output — bicepconfig.json was not discovered, so this linted against the CLI's "
            "built-in defaults. On a container backend the image is stale; on ACAS the disk "
            "image is a snapshot and has to be re-imported under a new tag. See #308"
        )

    # A severity rendered at all — the level is half of what an agent acts on, and a run that
    # reported none is not echoing what the tool returned. Coarse on purpose and kept separate
    # from the check above: it matches the word anywhere, so the model's prose satisfies it.
    #
    # It was once believed to hold whatever rule set ran, on the grounds that main.bicep yields
    # an error either way. That is wrong, and it is why the config check above had to be added
    # rather than leaned on this: `BCP035` is a `warning` in current Bicep, so `no-unused-params`
    # promoted by the config is the *only* error a healthy run has.
    if not re.search(r"\berror\b", output, re.IGNORECASE):
        failures.append(
            "no 'error' severity anywhere in the output — a healthy run reports at least one"
        )

    disposed = _DISPOSED.search(output)
    if disposed is None:
        failures.append("no 'Disposed N sandbox(es)' line — the sample did not run to completion")
    elif int(disposed.group(1)) < 1:
        failures.append(
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so nothing was validated in one"
        )

    return failures


def main(argv: list[str]) -> int:
    """CLI entry: read the sample output from a file or stdin, run ``assess``, and print OK or FAIL."""
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
            "FAIL: the live sample run did not verify the published stack:",
            file=sys.stderr,
        )
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(
        "OK  the Bicep sample validated main.bicep against the published wheels and a live sandbox"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
