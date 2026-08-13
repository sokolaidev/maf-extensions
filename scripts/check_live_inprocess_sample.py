"""Assert that a live `samples/09_inprocess_bicep` run actually validated the file.

The live workflow installs the *published* wheels, runs the sample against a real Azure OpenAI
model, and pipes its output here. This script is the assertion: it proves the scripted SARIF
survived the round trip through router, in-process fake, workload and agent — the whole point
of the run — without pinning anything that drifts on its own.

    python samples/09_inprocess_bicep/agent.py | tee out.txt
    python scripts/check_live_inprocess_sample.py out.txt   # or: ... | python scripts/check_live_inprocess_sample.py

The match is deliberately loose. The finding's message text is prose the model may rewrap, so
the rule id and severity are matched instead — `no-hardcoded-location` is an opaque token the
model is instructed to echo verbatim, so its presence is evidence the scripted SARIF reached
the workload, was parsed, was rendered and came back through the agent, not that the model
agreed with itself. The `warning` level is matched the same way: the level is half of what an
agent acts on, and a run that dropped it would read as clean.

Exits non-zero listing every reason it failed. A run that never created a sandbox
(`Disposed 0`) or never produced this diagnostic is a broken stack, not a clean file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The one finding the fake scripts `bicep lint` to return: `no-hardcoded-location`, a
#: warning against the hardcoded `location: 'eastus'` in main.bicep. Its rule id is an opaque
#: token the model is told to echo verbatim, so requiring it is strong evidence the scripted
#: SARIF came back through the whole seam. `bicep build` is scripted clean and is not asserted
#: on — its "no diagnostics" line is the absence of a token, which a broken stack produces too.
_REQUIRED_RULE = "no-hardcoded-location"

_DISPOSED = re.compile(r"Disposed\s+(\d+)\s+sandbox", re.IGNORECASE)


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    if _REQUIRED_RULE not in output:
        failures.append(
            f"diagnostic {_REQUIRED_RULE!r} is not in the output — the scripted SARIF did not come back"
        )

    # A severity must render — the level is half of what an agent acts on. `warning` is the
    # level the fake scripts, and the model is told to echo severities verbatim, so requiring
    # it proves the level survived the round trip rather than the model dropping it.
    if not re.search(r"\bwarning\b", output, re.IGNORECASE):
        failures.append(
            "no 'warning' severity anywhere in the output — the finding's level did not render"
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
        "OK  sample 09 validated main.bicep against the published wheels and the in-process fake"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
