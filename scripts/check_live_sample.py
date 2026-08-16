"""Assert that a live Bicep-sample run actually validated the file.

The live workflow installs the *published* wheels, runs one of the Bicep samples —
`samples/01_acas_bicep` against Azure, `samples/05_docker_bicep` in a local container,
`samples/09_inprocess_bicep` in-process — and pipes its output here. They all compile the
same `main.bicep` with the same CLI, so the same two rule ids come back whatever ran them;
one assertion serves every Bicep sample. `samples/02_wslc_bicep` prints the same shape and has
no job, because its guest needs a Windows runner with WSL.

    python samples/01_acas_bicep/agent.py | tee out.txt
    python scripts/check_live_sample.py out.txt   # or: ... | python scripts/check_live_sample.py

**Every diagnostic read here comes out of the block the sample prints from the tool result, and
none of it out of the model's reply.** That is the whole design (#314). `main.bicep` states both
rule ids, both severities and both line numbers in its own comments, so a model that read the
file and never called `bicep_validate` could write a summary satisfying any assertion made over
prose — and the same looseness failed three healthy releases from the other side, when a run
rendered `**error**` where the pattern wanted `[error]`. The compiler's rendering is fixed:
`build(name): N diagnostic(s)` then `[level] rule @ file:line: message`. Reading that instead
answers both directions at once, and needs no guesses about markup.

What is still read loosely, on purpose: the reply has to *name* the rules the block reports,
which is the claim that the diagnostics reached the model. Rule ids are opaque tokens, so a bare
substring is drift-proof in a way a rendered severity is not.

Two things are matched rather than compared whole. The diagnostics carry a day count and an
API-version list that climb with no code change (see the sample README), so nothing here reads a
message. And the config check has to stay an either-or: it asserts the compiler found
`bicepconfig.json`, because nothing else can. A guest carrying that file at a work-dir root the
tool no longer writes to lints against the CLI's built-in defaults and still satisfies every
other assertion here — same rule ids, same sandbox, weaker rule set (#308).

Exits non-zero listing every reason it failed. A run that never created a sandbox
(`Disposed 0`) or never produced these diagnostics is a broken stack, not a clean file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Every line read below comes off one the *sample* tagged. The model answers into the same
#: stream, so an unmarked search finds a reply quoting "compiles that reached the sandbox: 0"
#: before the sample's own count. `MEASURED` in `samples/*/_scaffold.py` writes the tag, and
#: `quoted` there takes it away from anything the model said.
#:
#: The tag matches case-sensitively — `(?-i:…)` — while the phrase after it keeps `_F`'s
#: `IGNORECASE`. The samples emit one fixed spelling, so accepting others only widens what has
#: to be sanitized, and a reader broader than its sanitizer is a hole rather than tolerance.
_M = r"^  (?-i:\[measured\]) "
_F = re.MULTILINE | re.IGNORECASE

#: Rule ids `main.bicep` always produces — a lint finding and a build finding. Neither says
#: which rule set ran; `config_was_discovered` is for that.
_REQUIRED_RULES = ("no-unused-params", "BCP035")

#: The rule the config switches on. Its *message* drifts — the day count climbs — but the id
#: does not, and the pinned `2023-01-01` only ages further past the threshold, so it fires more
#: over time, never less.
_CONFIG_RULE = "use-recent-api-versions"

#: The block the sample prints from what `bicep_validate` returned, and the tagged line that
#: closes it.
_HEADING = re.compile(r"==\s*Diagnostics as bicep_validate returned them\s*==")
_COMPILES = re.compile(_M + r"compiles that reached the sandbox:\s*(\d+)", _F)

#: How the compiler's own output renders, one phase per line and one diagnostic per line under
#: it. Fixed by `maf_sandbox_bicep._sarif.format_diagnostics`, not by anything a model chooses.
_PHASE = re.compile(r"^\s*(build|lint)\([^)]*\):\s*(no diagnostics|\d+ diagnostic)", re.MULTILINE)
_DIAGNOSTIC = re.compile(r"^\s*\[(\w+)\]\s+(\S+)\s+@", re.MULTILINE)

#: Tagged, so a model writing "Disposed 1 sandbox(es)." into its reply does not answer for the
#: router. This line is the sample's own report of what `dispose_scope` returned.
_DISPOSED = re.compile(_M + r"Disposed\s+(\d+)\s+sandbox", _F)


def _split(output: str) -> tuple[str, str, int] | None:
    """The model's reply, the tool's own output, and the count that closes it.

    ``None`` when there is no block to read. The closing line carries `[measured]`, which the
    sample takes away from anything the model said before printing it, so exactly one can exist
    in a healthy run — and a second is reason enough to trust none of them. That is what makes
    the fence a fence: a model is free to write the heading, and to write plausible diagnostics
    under it, and it cannot close the block.

    The reply is everything before the heading, and the block everything between. The *last*
    heading before the closing line is the sample's, so a reply that quoted the heading leaves
    its own text in the reply half, where it belongs.
    """
    closes = list(_COMPILES.finditer(output))
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


def diagnostics(block: str) -> dict[str, set[str]]:
    """Every rule the compiler reported in ``block``, and the severities it reported it at."""
    reported: dict[str, set[str]] = {}
    for match in _DIAGNOSTIC.finditer(block):
        reported.setdefault(match.group(2), set()).add(match.group(1).lower())
    return reported


def config_was_discovered(output: str) -> bool:
    """Whether the compiler's own diagnostics show it found `bicepconfig.json`.

    Either tell suffices on purpose: the config switches `_CONFIG_RULE` on and promotes
    `no-unused-params` from its built-in `warning` to `error`. They are one fact seen twice and
    vanish together, so requiring both would only add false reds — and a run that reports a
    subset of its rules is caught by the required-rules check instead.
    """
    split = _split(output)
    if split is None:
        return False
    return _config_tells(diagnostics(split[1]))


def _config_tells(reported: dict[str, set[str]]) -> bool:
    """The either-or itself, over diagnostics already read."""
    return _CONFIG_RULE in reported or "error" in reported.get("no-unused-params", set())


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    split = _split(output)
    if split is None:
        return [
            "the run printed no block of what bicep_validate returned — every claim about the "
            "compiler is then the model's own account of it, which is what this sample exists "
            "to avoid (#314)"
        ] + _assess_disposal(output)

    reply, block, compiles = split
    failures = _assess_compiles(block, compiles)
    failures.extend(_assess_reply(reply, block))
    failures.extend(_assess_disposal(output))
    return failures


def _assess_compiles(block: str, compiles: int) -> list[str]:
    """What the compiler said, read from the compiler."""
    failures: list[str] = []

    if compiles < 1:
        failures.append(
            "no bicep_validate call reached the sandbox — the tool refuses a call before it "
            "acquires anything when the file it was asked for is not one it can see, so this is "
            "a run that answered from the model alone"
        )

    phases = {match.group(1).lower() for match in _PHASE.finditer(block)}
    if phases != {"build", "lint"}:
        failures.append(
            f"the compile reported {sorted(phases) or 'no'} phase(s), expected both build and "
            "lint — a file can pass one and fail the other, and the two required rules are one "
            "from each"
        )

    reported = diagnostics(block)
    for rule in _REQUIRED_RULES:
        if rule not in reported:
            failures.append(
                f"the compiler did not report {rule!r} — main.bicep produces it on every run, so "
                "its absence is a broken stack rather than a clean file"
            )

    if not any("error" in levels for levels in reported.values()):
        failures.append(
            "no diagnostic came back at [error] — main.bicep has one, and the level is half of "
            "what an agent acts on"
        )

    if not _config_tells(reported):
        failures.append(
            f"no diagnostic reports {_CONFIG_RULE!r}, and none reports no-unused-params at "
            "[error] — bicepconfig.json was not discovered, so this linted against the CLI's "
            "built-in defaults. It is missing from the work-dir root of whatever served the "
            "run: an imported disk image (re-import under a new tag — overwriting the old one "
            "changes nothing), an image built from images/bicep-sandbox, or the backend's "
            "seed files. See #308"
        )
    return failures


def _assess_reply(reply: str, block: str) -> list[str]:
    """The diagnostics have to reach the model, not merely the log.

    Held to what the block actually reports rather than to `_REQUIRED_RULES`, so a run whose
    compiler reported one of them is not also failed here for the other. Rule ids are opaque
    tokens the sample tells the model to echo verbatim, so a bare substring is the right test:
    requiring a *rendered* severity is what failed three healthy releases, when one run wrote
    `**error**` and the pattern wanted `[error]`.
    """
    reported = diagnostics(block)
    said = reply.lower()
    missing = sorted(
        rule for rule in _REQUIRED_RULES if rule in reported and rule.lower() not in said
    )
    if not missing:
        return []
    return [
        f"the model's reply never names {', '.join(missing)} — the compiler reported it and the "
        "sample asks for every diagnostic back, so it reached the log without reaching the answer"
    ]


def _assess_disposal(output: str) -> list[str]:
    disposed = _DISPOSED.search(output)
    if disposed is None:
        return ["no measured 'Disposed N sandbox(es)' line — the sample did not run to completion"]
    if int(disposed.group(1)) < 1:
        return [
            "'Disposed 0 sandbox(es)' — no sandbox was ever created, so nothing was validated in one"
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
        "OK  the Bicep sample validated main.bicep against the published wheels and a live sandbox"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
