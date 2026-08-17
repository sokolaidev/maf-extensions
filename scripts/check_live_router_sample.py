"""Assert that a live `samples/11_router_two_backends` run exercised the real router.

    python samples/11_router_two_backends/agent.py | tee out.txt
    python scripts/check_live_router_sample.py out.txt   # or: ... | python …

Like the host-tools check and unlike the rest, this matches exactly: no model stands between
the library and stdout, so the printed values are `maf-sandbox`'s own answers.

The load-bearing assertion is the disposal count. A sandbox is acquired on each of the two
registered backends and only one of them is serving, so `dispose_scope` returning **2** is the
whole claim that disposal fans out. A router that reached only the serving backend would
return 1, leave the other sandbox running, and every other line here would still be correct.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

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

#: `Allowlisted host answered HTTP 200; an unlisted one answered HTTP 000.` Both are read back
#: from what curl reported inside the sandbox.
_EGRESS = re.compile(
    r"Allowlisted host answered HTTP\s+(\d+);\s+an unlisted one answered HTTP\s+(\d+)",
    re.IGNORECASE,
)

#: `Disposed N sandbox(es) across M backends.` Both numbers are read back by the sample from
#: what it observed — the first from `dispose_scope`'s return, the second from the list it
#: registered — so these compare measurements, not literals the sample printed.
_FOOTER = re.compile(
    r"Completed\s+(\d+)\s+of\s+5\s+acts\.\s+Disposed\s+(\d+)\s+sandbox\(es\)\s+"
    r"across\s+(\d+)\s+backends",
    re.IGNORECASE,
)


def _line_containing(output: str, needle: str) -> str | None:
    """The first line holding ``needle``, or ``None``."""
    for line in output.splitlines():
        if needle in line:
            return line
    return None


def _resolved_name(line: str) -> str:
    """The backend name a selection line reports, from ``router.backend.name == 'x'``."""
    match = re.search(r"==\s*'([^']*)'", line)
    return match.group(1) if match else ""


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    for selected, expected in _SELECTION.items():
        line = _line_containing(output, f"selected={selected!r}")
        if line is None:
            failures.append(f"no line for selected={selected!r} — act 1 did not run it")
            continue
        resolved = _resolved_name(line)
        if resolved != expected:
            failures.append(
                f"selected={selected!r} resolved to {resolved!r}, expected exactly {expected!r} "
                "— `selected=` names the backend that serves, and it did not"
            )

    default = _line_containing(output, "selected omitted")
    if default is None:
        failures.append("no 'selected omitted' line — the registration-order default is unshown")
    elif _resolved_name(default) != _DEFAULT_BACKEND:
        failures.append(
            f"with `selected=` omitted the router resolved to {_resolved_name(default)!r}, "
            f"expected exactly {_DEFAULT_BACKEND!r} — the first backend the sample registers"
        )

    for exception in _REFUSALS:
        if exception not in output:
            failures.append(
                f"{exception} is not in the output — a spec the serving backend cannot meet was "
                "not refused, which would mean the router had rerouted or degraded"
            )

    executed = _line_containing(output, "it runs:")
    if executed is None:
        failures.append(
            "no 'it runs:' line — the router agreed a backend could serve and nothing proved "
            "one did"
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

    failures.extend(_assess_egress(output))
    failures.extend(_assess_footer(output))
    return failures


def _assess_egress(output: str) -> list[str]:
    """The allowlist pair, matched exactly.

    `200` and `000` are curl's own report from inside the sandbox, not the sample's account of
    it: the allowed host answered, and the unnamed one could not be connected to at all. Both
    halves are required, and the denied half is the load-bearing one — a proxy that allowed
    everything would still print a `2xx` for the allowed host and look identical here.
    """
    pair = _EGRESS.search(output)
    if pair is None:
        return [
            "no 'Allowlisted host answered HTTP N; an unlisted one answered HTTP N.' line — "
            "the allowlist act did not run, so nothing here shows egress was confined"
        ]
    allowed, denied = pair.groups()
    failures: list[str] = []
    if not allowed.startswith("2"):
        failures.append(
            f"the allowlisted host answered HTTP {allowed}, expected a 2xx — the allowlist "
            "denied a host it named, which is a broken allowlist rather than a confined one"
        )
    if denied != "000":
        failures.append(
            f"the unlisted host answered HTTP {denied}, expected exactly 000 — curl reports "
            "000 when no connection was made at all, and any status at all means the proxy "
            "opened a tunnel to a host the spec never named"
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
            f"only {acts} of 5 acts completed — act 4 skips itself when MAF_EGRESS_PROXY_IMAGE "
            "names no built proxy image, and a skipped allowlist act is the one result that "
            "reads exactly like a passing one"
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
        "OK  the router selected by name, refused what it could not serve, confined egress to "
        "the one host named, and disposed across both backends"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
