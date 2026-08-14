"""Assert that a live CodeAct sample run actually computed the answer.

Shared by `samples/03_acas_codeact` (a real Azure sandbox) and `samples/06_docker_codeact` (a
Docker container) — the task, the one right answer and the printed shape are identical, so one
checker serves both. The live workflow installs the *published* wheels, runs the sample, and
pipes its output here. `agent.py` prints exactly two things: the model's reply
(`response.text`) and the disposal line — never `execute_code`'s own tool result — so this
script has no `stdout:` marker to look for and does not try.

    python samples/03_acas_codeact/agent.py | tee out.txt
    python scripts/check_live_codeact_sample.py out.txt   # or: ... | python scripts/check_live_codeact_sample.py

What it checks instead: the exact value `354224848179261915075` (the 100th Fibonacci number —
the sample's task has exactly one right answer) is present, and a `Disposed N sandbox(es).`
line reports N >= 1. A sandbox is only ever created when `execute_code` actually runs, so
`Disposed 1` (or more) is proof the tool was called, and `Disposed 0` means the model answered
without running any code — the T0 behaviour this sample exists to contrast with. Neither fact
is much evidence alone: a model can recite the right Fibonacci number from training data
regardless of whether a sandbox happened to spin up. Together they are stronger: a run that
disposed zero sandboxes but still printed the right number, or one that disposed a sandbox but
printed the wrong number, both fail this check, and a healthy run does neither.

What this does NOT prove: the sample prints only the model's reply, not the tool result, so
this script cannot confirm the printed number is literally what `execute_code` returned rather
than what the model wrote afterward — only that a sandbox ran and the right value showed up in
the output.

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
        "OK  the CodeAct sample computed the 100th Fibonacci number in a live sandbox against "
        "the published wheels"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
