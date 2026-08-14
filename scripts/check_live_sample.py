"""Assert that a live `samples/01_acas_bicep` run actually validated the file.

The live workflow installs the *published* wheels, runs the sample against a real Azure
sandbox, and pipes its output here. This script is the assertion: it proves the compiler's
diagnostics survived the round trip through router, backend, image and workload — the whole
point of the run — without pinning anything that drifts on its own.

    python samples/01_acas_bicep/agent.py | tee out.txt
    python scripts/check_live_sample.py out.txt   # or: ... | python scripts/check_live_sample.py

The match is deliberately loose. Diagnostics carry a day count and an API-version list that
climb with no code change (see the sample README), so a whole-string comparison would become
the very drift it is meant to catch. Rule ids and severities are matched instead — the rule
ids are opaque tokens the model is instructed to echo verbatim, so their presence is evidence
the compiler ran and its findings reached the end, not that the model agreed with itself.

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
#: its findings reached the end. `use-recent-api-versions` is produced too but not required —
#: it is the one the README calls out as drifting, and the point is to not depend on it.
_REQUIRED_RULES = ("no-unused-params", "BCP035")

_DISPOSED = re.compile(r"Disposed\s+(\d+)\s+sandbox", re.IGNORECASE)


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    for rule in _REQUIRED_RULES:
        if rule not in output:
            failures.append(
                f"diagnostic {rule!r} is not in the output — the compiler's findings did not come back"
            )

    # A severity must render — the level is half of what an agent acts on. `error` specifically:
    # a healthy run always reports at least one (main.bicep's faults yield an error either way),
    # so requiring it does not depend on the drift-prone warning. It is deliberately not tied to
    # a particular rule — the model's prose is not parsed finely enough to assert which rule the
    # error belongs to — so this proves severities render, not that any single rule is at error.
    if not re.search(r"\berror\b", output, re.IGNORECASE):
        failures.append(
            "no 'error' severity anywhere in the output — a healthy run reports at least one"
        )

    disposed = _DISPOSED.search(output)
    if disposed is None:
        failures.append(
            "no 'Disposed N sandbox(es)' line — the sample did not run to completion"
        )
    elif int(disposed.group(1)) < 1:
        failures.append(
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so nothing was validated in one"
        )

    return failures


def main(argv: list[str]) -> int:
    """CLI entry: read the sample output from a file or stdin, run ``assess``, and print OK or FAIL."""
    if len(argv) > 2:
        print(
            f"usage: {argv[0]} [output-file]  (reads stdin if omitted)", file=sys.stderr
        )
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
        "OK  sample 01 validated main.bicep against the published wheels and a live sandbox"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
