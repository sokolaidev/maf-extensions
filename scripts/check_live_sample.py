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

It also checks the compiler found `bicepconfig.json`, because nothing else can. A guest
carrying that file at a work-dir root the tool no longer writes to lints against the CLI's
built-in defaults and still satisfies every other assertion here — same rule ids, same
sandbox, weaker rule set (#308).

Exits non-zero listing every reason it failed. A run that never created a sandbox
(`Disposed 0`) or never produced these diagnostics is a broken stack, not a clean file.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Rule ids `main.bicep` always produces — a lint finding and a build finding. Opaque tokens
#: the model is told to echo verbatim, so their presence is evidence the compiler ran. Neither
#: says which rule set ran; `_CONFIG_TELLS` is for that.
_REQUIRED_RULES = ("no-unused-params", "BCP035")


def _reported(rule: str, level: str = r"error|warning|info|note") -> re.Pattern[str]:
    """Match ``rule`` *rendered as a diagnostic* — a bracketed severity beside it, either order.

    Naming a rule is not reporting it: without the config a model can truthfully write
    "use-recent-api-versions is missing", and a substring test reads that as present. Only
    punctuation may sit between the two halves, so neither a negation nor a severity belonging
    to another diagnostic on the same line can stand in for the real thing.
    """
    level_re = rf"\[(?:{level})\]"
    gap = r"[^\w\n]*"  # `- `, `| `, backticks — never a newline, and never another word
    return re.compile(
        rf"{level_re}{gap}{re.escape(rule)}|{re.escape(rule)}{gap}{level_re}", re.IGNORECASE
    )


#: The rule the config switches on. Its *message* drifts — the day count climbs — but the id
#: does not, and the pinned `2023-01-01` only ages further past the threshold, so it fires more
#: over time, never less.
_CONFIG_RULE = "use-recent-api-versions"

#: Either one proves `bicepconfig.json` was found: the config switches `_CONFIG_RULE` on, and
#: promotes `no-unused-params` from its built-in `warning` to `error`.
_CONFIG_TELLS = (_reported(_CONFIG_RULE), _reported("no-unused-params", "error"))

_DISPOSED = re.compile(r"Disposed\s+(\d+)\s+sandbox", re.IGNORECASE)


def config_was_discovered(output: str) -> bool:
    """Whether the diagnostics show the compiler found `bicepconfig.json`.

    Either tell suffices on purpose: they are one fact seen twice and vanish together, so
    requiring both would only add false reds when a model reformats one of them away.
    """
    return any(tell.search(output) for tell in _CONFIG_TELLS)


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    for rule in _REQUIRED_RULES:
        if rule not in output:
            failures.append(
                f"diagnostic {rule!r} is not in the output — the compiler's findings did not come back"
            )

    # Which rule set ran. Everything above passes against the CLI's built-in defaults (#308).
    if not config_was_discovered(output):
        failures.append(
            f"no diagnostic reports {_CONFIG_RULE!r}, and none reports no-unused-params at "
            "[error] — bicepconfig.json was not discovered, so this linted against the CLI's "
            "built-in defaults. It is missing from the work-dir root of whatever served the "
            "run: an imported disk image (re-import under a new tag — overwriting the old one "
            "changes nothing), an image built from images/bicep-sandbox, or the backend's "
            "seed files. See #308"
        )

    # A severity rendered at all — the level is half of what an agent acts on. Coarse by
    # design, and not a config check: it matches the word anywhere, so prose satisfies it.
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
