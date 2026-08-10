"""Assert that a live `samples/03_acas_codeact` run actually computed the answer.

The live workflow installs the *published* wheels, runs the sample against a real Azure
sandbox, and pipes its output here. This script is the assertion: it proves the answer came
from a Python interpreter running inside the sandbox (T2) — via `execute_code`'s own result
format — rather than from the model predicting it from training data (T0), which is the whole
point of the run.

    python samples/03_acas_codeact/agent.py | tee out.txt
    python scripts/check_live_codeact_sample.py out.txt   # or: ... | python scripts/check_live_codeact_sample.py

The match here is exact, unlike `check_live_sample.py`'s. That script matches rule ids and
severities instead of whole strings because sample 01's diagnostics carry a day count and an
API-version list that climb with no code change. Sample 03's task has exactly one right answer,
`354224848179261915075` (the 100th Fibonacci number), and nothing in a healthy run's output
drifts on its own — so requiring the literal value is not the drift-prone comparison a whole-
string match would be for sample 01; it is the strongest evidence available.

Exits non-zero listing every reason it failed. A run that never created a sandbox
(`Disposed 0`) or never produced the value is a broken stack, not a clean file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The 100th Fibonacci number, F(0) = 0, F(1) = 1 — the sample's task has exactly one right
#: answer, so the literal value is required rather than a looser pattern.
_ANSWER = "354224848179261915075"

_DISPOSED = re.compile(r"Disposed\s+(\d+)\s+sandbox", re.IGNORECASE)


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    if _ANSWER not in output:
        failures.append(
            f"{_ANSWER!r} is not in the output — the 100th Fibonacci number did not come back"
        )

    # `stdout:` is `execute_code`'s own result-format marker (see `_format_result` in
    # `maf-sandbox-codeact`), rendered before whatever the program printed. Its presence is
    # evidence the value came through the tool's formatting, not the model's prose alone.
    if "stdout:" not in output:
        failures.append(
            "no 'stdout:' marker in the output — execute_code's result format did not appear, "
            "so the answer (if present) cannot be attributed to a real run"
        )

    disposed = _DISPOSED.search(output)
    if disposed is None:
        failures.append(
            "no 'Disposed N sandbox(es)' line — the sample did not run to completion"
        )
    elif int(disposed.group(1)) < 1:
        failures.append(
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so nothing was computed in one"
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
        "OK  sample 03 computed the 100th Fibonacci number in a live sandbox against the "
        "published wheels"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
