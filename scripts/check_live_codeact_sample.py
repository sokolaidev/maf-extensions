"""Assert that a live CodeAct sample run actually computed the answer.

Shared by `samples/03_acas_codeact` (a real Azure sandbox) and `samples/06_docker_codeact` (a
Docker container) — the task, the one right answer and the printed shape are identical, so one
checker serves both. `samples/04_wslc_codeact` prints the same shape and has no job, because its
guest needs a Windows runner with WSL. The live workflow installs the *published* wheels, runs
the sample, and pipes its output here.

    python samples/03_acas_codeact/agent.py | tee out.txt
    python scripts/check_live_codeact_sample.py out.txt   # or: ... | python …

**The answer is read out of the block the sample prints from the tool result, never out of the
model's reply.** The 100th Fibonacci number is a constant any model can recite, so a reply
carrying it is evidence of nothing; the same digits inside `execute_code`'s own output are
evidence a program ran in the sandbox and printed them (#314). The sample prints that block from
what the framework recorded next to the call, so the model does not write it.

Three things together, and each is weak alone: `execute_code` returned output at all — a call it
refuses answers with an `Error:` string and never reaches the interpreter; that output carries
the one right answer; and the router disposed at least one sandbox, on a line the sample tagged
rather than one a model could write. `Disposed 0` means no sandbox was ever created, which is
the T0 behaviour these samples exist to contrast with.

What this still does not prove: a program that prints the constant rather than computing it ran
in the sandbox just the same, and its output is indistinguishable here. The gap this closes is
"no code ran at all", not "the code did arithmetic".

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Every line read below comes off one the *sample* tagged. The model answers into the same
#: stream, so an unmarked search finds a reply quoting "Disposed 1 sandbox(es)." before the
#: sample's own line. `MEASURED` in `samples/*/_scaffold.py` writes the tag, and `quoted` there
#: takes it away from anything the model said. Case-sensitive on the tag, lax after it: a reader
#: broader than its sanitizer is a hole rather than tolerance.
_M = r"^  (?-i:\[measured\]) "
_F = re.MULTILINE | re.IGNORECASE

#: The 100th Fibonacci number, F(0) = 0 and F(1) = 1 — the sample's task has exactly one right
#: answer, so the literal value is required rather than a looser pattern.
_ANSWER = "354224848179261915075"

#: The block the sample prints from what `execute_code` returned, and the tagged line closing it.
_HEADING = re.compile(r"==\s*Program output as execute_code returned it\s*==")
_RUNS = re.compile(_M + r"programs whose output came back from the sandbox:\s*(\d+)", _F)

#: The section header the tool puts above a program's stdout. Fixed by
#: `maf_sandbox_codeact._tool._format_result`, not by anything a model chooses.
_STDOUT = re.compile(r"^\s*stdout:\s*$", re.MULTILINE)

#: Tagged, so a model writing "Disposed 1 sandbox(es)." into its reply does not answer for the
#: router. This line is the sample's own report of what `dispose_scope` returned.
_DISPOSED = re.compile(_M + r"Disposed\s+(\d+)\s+sandbox", _F)


def _split(output: str) -> tuple[str, str, int] | None:
    """The model's reply, the tool's own output, and the count that closes it.

    ``None`` when there is no block to read. The closing line carries `[measured]`, which the
    sample takes away from anything the model said before printing it, so exactly one can exist
    in a healthy run — and a second is reason enough to trust none of them. That is what makes
    the fence a fence: a model is free to write the heading, and to write the right number under
    it, and it cannot close the block.

    The reply is everything before the heading, and the block everything between. The *last*
    heading before the closing line is the sample's, so a reply that quoted the heading leaves
    its own text in the reply half, where it belongs.
    """
    closes = list(_RUNS.finditer(output))
    if len(closes) != 1:
        return None
    opened = list(_HEADING.finditer(output, 0, closes[0].start()))
    if not opened:
        return None
    return (
        output[: opened[-1].start()],
        output[opened[-1].end() : closes[0].start()],
        int(closes[0].group(1)),
    )


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    split = _split(output)
    if split is None:
        return [
            "the run printed no block of what execute_code returned — the number in the reply is "
            "then a constant the model could recite, which is what this sample exists to rule "
            "out (#314)"
        ] + _assess_disposal(output)

    reply, block, runs = split
    failures = _assess_run(block, runs)
    failures.extend(_assess_reply(reply))
    failures.extend(_assess_disposal(output))
    return failures


def _assess_run(block: str, runs: int) -> list[str]:
    """What the interpreter printed, read from the interpreter."""
    failures: list[str] = []

    if runs < 1:
        failures.append(
            "no execute_code call came back with a program's output — the tool answers a call it "
            "refuses with an `Error:` string, without reaching the interpreter, so this is a run "
            "that answered from the model alone"
        )

    if _STDOUT.search(block) is None:
        failures.append(
            "the tool's output carries no `stdout:` section — the task is to print the number, "
            "so a run that printed nothing did not perform it"
        )

    if _ANSWER not in block:
        failures.append(
            f"{_ANSWER!r} is not in what execute_code returned — the 100th Fibonacci number did "
            "not come back from the sandbox"
        )
    return failures


def _assess_reply(reply: str) -> list[str]:
    """The answer has to reach the model, not merely the log.

    The value is the one thing this sample asks the model to carry back, and the sample's
    instructions say to report exactly what the tool returned. A reply that computed something
    else while the tool printed the right number is a broken round trip, not a pass.
    """
    if _ANSWER in reply:
        return []
    return [
        f"the model's reply never carries {_ANSWER!r} — the sandbox printed it and the sample "
        "asks for exactly what the tool returned, so it reached the log without reaching the "
        "answer"
    ]


def _assess_disposal(output: str) -> list[str]:
    disposed = _DISPOSED.search(output)
    if disposed is None:
        return ["no measured 'Disposed N sandbox(es)' line — the sample did not run to completion"]
    if int(disposed.group(1)) < 1:
        return [
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so nothing was computed in one"
        ]
    return []


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
        "OK  the CodeAct sample computed the 100th Fibonacci number in a live sandbox against "
        "the published wheels"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
