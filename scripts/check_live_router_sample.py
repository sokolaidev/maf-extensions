"""Assert that a live `samples/11_router_two_backends` run exercised the real router.

    python samples/11_router_two_backends/agent.py | tee out.txt
    python scripts/check_live_router_sample.py out.txt   # or: ... | python …

Two assertions carry this file, and they are load-bearing for different reasons.

The **disposal count**. A sandbox is acquired on each of the two registered backends and only
one of them is serving, so `dispose_scope` returning **2** is the whole claim that disposal fans
out. A router that reached only the serving backend would return 1, leave the other sandbox
running, and every other line here would still be correct.

The **restore pair**. Act 4 runs one agent against one file under two egress postures, and both
outcomes are required: `FAILED` closed, `RESTORED` allowlisted. Either alone is consistent with
a sandbox that confines nothing — a container with the host's network would restore under both,
and a container that could not start would fail under both. Only the pair says the deployment's
wiring is what decided it.

Every line this file reads carries the `[measured]` tag, and act 4 puts a model's prose into the
same stream. The sample runs that prose through `quoted()`, which prefixes `> ` to any line
impersonating the tag — so a forged measurement is a quotation here and matches nothing.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The prefix on every line the sample vouches for. `_scaffold.quoted` has already taken it
#: away from anything the model said, so requiring it is what keeps act 4's reply from being
#: able to answer any question below.
_TAG = "[measured]"

#: Which backend `selected=` resolves to, and the name it must resolve to. `docker` and the
#: in-process backend declare different isolation and different capabilities, so a swap here is
#: a change in what the router chose rather than a cosmetic difference.
_SELECTION = {"in-process": "in-process", "docker": "docker"}

#: With `selected=` omitted, registration order decides. The sample registers the in-process
#: backend first, and the line saying so is the only place that behaviour is visible.
_DEFAULT_BACKEND = "in-process"

#: Both refusals, by exception class name — API, where the sentences are not. Each fires with a
#: backend that *could* have served the spec registered alongside and unused, which is the
#: sample's subject: the router resolves once and never reconsiders.
_REFUSALS = ("SandboxBackendNotPermitted", "SandboxCapabilityNotSupported")

#: What the container printed. Proof the selected backend really ran the command rather than
#: the router merely agreeing that it could.
_EXECUTED = "routed"

#: `[measured] AVM restore under egress closed: FAILED`, and its allowlisted twin. The verdicts
#: are read back from whether the compiler reported BCP192, not from what the sample hoped for.
_RESTORE = re.compile(
    rf"^\s*{re.escape(_TAG)}\s+AVM restore under egress\s+(closed|allowlist):\s+(\w+)",
    re.MULTILINE,
)

#: What each posture has to report. Closed must fail, because a container with no route out
#: cannot download a module; allowlisted must restore, because the four hosts the kind names
#: are exactly what the download needs.
_RESTORE_EXPECTED = {"closed": "FAILED", "allowlist": "RESTORED"}

#: `Disposed N sandbox(es) across M backends.` Both numbers are read back by the sample from
#: what it observed — the first from `dispose_scope`'s return, the second from the list it
#: registered — so these compare measurements, not literals the sample printed.
_FOOTER = re.compile(
    rf"{re.escape(_TAG)}\s+Completed\s+(\d+)\s+of\s+5\s+acts\.\s+Disposed\s+(\d+)\s+"
    r"sandbox\(es\)\s+across\s+(\d+)\s+backends",
    re.IGNORECASE,
)


def _measured_line(output: str, needle: str) -> str | None:
    """The first tagged line holding ``needle``, stripped, or ``None``.

    Tagged, not merely containing. Act 4 prints a model's reply into this stream, and a reply
    is free to contain `it runs: 'routed'`; it is not free to contain a line that *starts* with
    the tag, because the sample quotes those away before printing them.
    """
    for line in output.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(_TAG) and needle in stripped:
            return stripped
    return None


def _resolved_name(line: str) -> str:
    """The backend name a selection line reports, from ``router.backend.name == 'x'``."""
    match = re.search(r"==\s*'([^']*)'", line)
    return match.group(1) if match else ""


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    for selected, expected in _SELECTION.items():
        line = _measured_line(output, f"selected={selected!r}")
        if line is None:
            failures.append(f"no measured line for selected={selected!r} — act 1 did not run it")
            continue
        resolved = _resolved_name(line)
        if resolved != expected:
            failures.append(
                f"selected={selected!r} resolved to {resolved!r}, expected exactly {expected!r} "
                "— `selected=` names the backend that serves, and it did not"
            )

    default = _measured_line(output, "selected omitted")
    if default is None:
        failures.append("no 'selected omitted' line — the registration-order default is unshown")
    elif _resolved_name(default) != _DEFAULT_BACKEND:
        failures.append(
            f"with `selected=` omitted the router resolved to {_resolved_name(default)!r}, "
            f"expected exactly {_DEFAULT_BACKEND!r} — the first backend the sample registers"
        )

    for exception in _REFUSALS:
        if _measured_line(output, exception) is None:
            failures.append(
                f"{exception} is not on a measured line — a spec the serving backend cannot "
                "meet was not refused, which would mean the router had rerouted or degraded"
            )

    executed = _measured_line(output, "it runs:")
    if executed is None:
        failures.append(
            "no measured 'it runs:' line — the router agreed a backend could serve and nothing "
            "proved one did"
        )
    else:
        # The quoted value, compared whole. A substring test accepts `'unrouted'` and
        # `'routed with an error'`, which are a container answering wrongly rather than a
        # container answering — the one thing this line exists to distinguish.
        printed = re.search(r"it runs:\s*'([^']*)'", executed)
        actual = printed.group(1) if printed else ""
        if actual != _EXECUTED:
            failures.append(
                f"the selected backend printed {actual!r}, expected exactly {_EXECUTED!r} — "
                "the command's output is what proves a container ran, so anything else is a "
                "different result rather than a looser match on the same one"
            )

    failures.extend(_assess_restore(output))
    failures.extend(_assess_footer(output))
    return failures


def _assess_restore(output: str) -> list[str]:
    """Act 4's two verdicts, both required, in the compiler's own terms.

    A `br/public:` module cannot be type-checked without downloading it, so whether the restore
    happened is the workload's own answer to whether its egress was there. Reading both postures
    is what makes it evidence: the same agent, the same file and the same spec ran twice, and
    the only difference between the runs was whether the deployment gave the container a proxy.
    """
    seen: dict[str, list[str]] = {}
    for posture, verdict in _RESTORE.findall(output):
        seen.setdefault(posture, []).append(verdict)

    failures: list[str] = []
    for posture, expected in _RESTORE_EXPECTED.items():
        verdicts = seen.get(posture, [])
        if not verdicts:
            failures.append(
                f"no 'AVM restore under egress {posture}' line — act 4 did not run that posture, "
                "so nothing here shows the deployment's wiring decided anything"
            )
        elif len(verdicts) > 1:
            # Counted rather than resolved, deliberately. The sample prints each verdict once,
            # so a second one came from somewhere else — and picking the first would read a
            # model's reply, while picking the last would read whatever came after it. Neither
            # is a measurement, so this refuses to choose.
            failures.append(
                f"'AVM restore under egress {posture}' appears {len(verdicts)} times, saying "
                f"{', '.join(verdicts)} — the sample prints it once, so something else is "
                "writing measured lines into this stream and none of them can be trusted"
            )
        elif verdicts[0] != expected:
            failures.append(
                f"the AVM module {verdicts[0]} under egress {posture}, expected {expected} — "
                + (
                    "a container with no route out cannot download a module, so restoring "
                    "means it had a route the deployment never gave it"
                    if posture == "closed"
                    else "the allowlist names the four hosts the download needs, so failing "
                    "means the proxy did not serve the list the workload asked for"
                )
            )
    return failures


def _assess_footer(output: str) -> list[str]:
    """The three counts, and the one that carries the sample's claim."""
    footer = _FOOTER.search(output)
    if footer is None:
        return [
            "no 'Completed N of 5 acts. Disposed N sandbox(es) across N backends.' line — the "
            "sample did not run to completion"
        ]
    acts, disposed, registered = (int(group) for group in footer.groups())
    failures: list[str] = []
    if acts != 5:
        failures.append(
            f"only {acts} of 5 acts completed — act 4 skips itself when any of its four "
            "variables is unset, and a skipped egress act is the one result that reads exactly "
            "like a passing one"
        )
    if registered != 2:
        failures.append(
            f"{registered} backends were registered, expected exactly 2 — the whole subject is "
            "a router holding more than one"
        )
    if disposed != 2:
        failures.append(
            f"dispose_scope disposed {disposed} sandbox(es), expected exactly 2 — one was "
            "acquired on each registered backend and only one of them was serving, so anything "
            "less means disposal reached the serving backend alone and left a sandbox running"
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
        print("FAIL: the router sample did not exercise the published behaviour:", file=sys.stderr)
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print(
        "OK  the router selected by name, refused what it could not serve, gave a real workload "
        "exactly the egress it asked for and nothing else, and disposed across both backends"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
