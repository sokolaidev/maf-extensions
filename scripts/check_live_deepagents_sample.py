"""Assert that a live Deep Agents run actually compiled the file.

The live workflow installs the *published* wheels, runs `samples/17_deepagents_docker_bicep`
and pipes its output here. That sample validates sample 05's `main.bicep` with sample 05's
image, so the compiler reports the same rules — but through Deep Agents' own `execute` tool,
whose command the **model** wrote.

    python samples/17_deepagents_docker_bicep/agent.py | tee out.txt
    python scripts/check_live_deepagents_sample.py out.txt   # or: ... | python ... -

**Every diagnostic read here comes out of the block the sample prints from the tool results,
and none of it out of the model's reply** (#314). What is read there is the compiler's SARIF:
each diagnostic is a `ruleId`, and a `level` beside it when it is not the default warning.

**Weaker than `check_live_sample.py`, and deliberately.** That one knows `bicep_validate` ran a
fixed argv, so it can require both compiler phases and read a rendering the kind controls. Here
the model chose the command, so the only thing a result proves is that *something* reached the
compiler: a result counting as a compile is one carrying SARIF `ruleId` entries, and nothing
stronger can be said from outside the model. What the rules themselves say is not weaker — the
file is the same, so the same ids and the same promoted level have to come back.

Two things are matched rather than compared whole. The diagnostics carry a day count and an
API-version list that climb with no code change, so nothing here reads a message. And the
config check is an either-or: it asserts the compiler found `bicepconfig.json`, because nothing
else can. A guest carrying that file where the tool no longer writes lints against the CLI's
built-in defaults and satisfies every other assertion here (#308).

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Every line read below comes off one the *sample* tagged. The model answers into the same
#: stream, so an unmarked search finds a reply quoting "compiles that reached the sandbox: 0"
#: before the sample's own count. `MEASURED` in `samples/*/_scaffold.py` writes the tag, and
#: `quoted` there takes it away from anything the model said.
_M = r"^  (?-i:\[measured\]) "
_F = re.MULTILINE | re.IGNORECASE

#: Rule ids `main.bicep` always produces — a lint finding and a build finding. Neither says
#: which rule set ran; `config_was_discovered` is for that.
_REQUIRED_RULES = ("no-unused-params", "BCP035")

#: The rule the config switches on. Its *message* drifts — the day count climbs — but the id
#: does not, and the pinned `2023-01-01` only ages further past the threshold.
_CONFIG_RULE = "use-recent-api-versions"

#: The block the sample prints from what `execute` returned, and the tagged line that closes it.
_HEADING = re.compile(r"==\s*Diagnostics as execute returned them\s*==")
_COMPILES = re.compile(_M + r"compiles that reached the sandbox:\s*(\d+)", _F)

#: One SARIF diagnostic, as the compiler writes it: a `ruleId`, and the `level` that follows it
#: when there is one. The gap between the two is `[^"]*` so it tolerates what stands between
#: them in this stream — a newline, the block's indent, and the adapter's `[stderr]` prefix —
#: while still refusing to cross into the next key: a diagnostic whose next quoted token is
#: `message` carries no level, and SARIF's default for that is `warning`.
_SARIF = re.compile(r'"ruleId":\s*"(?P<rule>[^"]+)"(?:,[^"]*"level":\s*"(?P<level>[^"]+)")?')

#: What SARIF means by omitting `level`, and what the sample's prompt tells the model it means.
_DEFAULT_LEVEL = "warning"

#: Tagged, so a model writing "Disposed 1 sandbox(es)." into its reply does not answer for the
#: router. This line is the sample's own report of what `dispose_scope` returned.
_DISPOSED = re.compile(_M + r"Disposed\s+(\d+)\s+sandbox", _F)
_NOT_DISPOSED = re.compile(_M + r"Not fully disposed:[^\r\n]*", _F)


def _split(output: str) -> tuple[str, str, int] | None:
    """The model's reply, the tool results, and the count that closes them.

    ``None`` when there is no block to read. The closing line carries `[measured]`, which the
    sample takes away from anything the model said before printing it, so exactly one can exist
    in a healthy run — and a second is reason enough to trust none of them. That is what makes
    the fence a fence: a model is free to write the heading, and to write plausible SARIF under
    it, and it cannot close the block.

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
    """Every rule the compiler reported in ``block``, and the levels it reported it at."""
    reported: dict[str, set[str]] = {}
    for match in _SARIF.finditer(block):
        reported.setdefault(match.group("rule"), set()).add(
            (match.group("level") or _DEFAULT_LEVEL).lower()
        )
    return reported


def config_was_discovered(output: str) -> bool:
    """Whether the compiler's own diagnostics show it found `bicepconfig.json`.

    Either tell suffices on purpose: the config switches `_CONFIG_RULE` on and promotes
    `no-unused-params` from its built-in `warning` to `error`. They are one fact seen twice and
    vanish together, so requiring both would only add false reds.
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
            "the run printed no block of what execute returned — every claim about the compiler "
            "is then the model's own account of it, which is what this sample exists to avoid "
            "(#314)"
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
            "no execute result carried a SARIF diagnostic — the model either never ran the "
            "compiler or ran something else, so this is a run that answered from the model alone"
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
            "no diagnostic came back at level error — main.bicep has one, and the level is half "
            "of what an agent acts on"
        )

    if not _config_tells(reported):
        failures.append(
            f"no diagnostic reports {_CONFIG_RULE!r}, and none reports no-unused-params at level "
            "error — bicepconfig.json was not discovered, so this linted against the CLI's "
            "built-in defaults. It is missing from the work-dir root of whatever served the run: "
            "an image built from images/bicep-sandbox, or the backend's seed files. See #308"
        )
    return failures


def _assess_reply(reply: str, block: str) -> list[str]:
    """The diagnostics have to reach the model, not merely the log.

    Held to what the block actually reports rather than to `_REQUIRED_RULES`, so a run whose
    compiler reported one of them is not also failed here for the other. Rule ids are opaque
    tokens the sample tells the model to echo verbatim, so a bare substring is the right test:
    requiring a *rendered* level is what failed three healthy releases of the sibling sample,
    when one run wrote `**error**` where the pattern wanted `[error]`.
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
    if _NOT_DISPOSED.search(output):
        return ["the scope purge could not account for every sandbox — data may remain"]
    return []


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
        print("FAIL: the live sample run did not verify the published stack:", file=sys.stderr)
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(
        "OK  the Deep Agents sample compiled main.bicep against the published wheels and a live "
        "sandbox"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
