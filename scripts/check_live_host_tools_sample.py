"""Assert that a live `samples/10_inprocess_host_tools` run exercised the real contract.

The live workflow installs the *published* wheels, runs the sample and pipes its output here.

This is the one check in the set that can match strictly, and the reason is worth stating: no
model stands between the library and stdout. Every other live check reads a model's retelling
of what a tool returned, so it matches rule ids and severities loosely and lives with the
looseness. Here the printed values *are* the library's answers — `registry.names()`, each leg
of `HostToolAggregate`, the class name of the exception the router raised — so a mismatch is a
behaviour change in `maf-sandbox`, not a paraphrase.

    python samples/10_inprocess_host_tools/agent.py | tee out.txt
    python scripts/check_live_host_tools_sample.py out.txt   # or: ... | python …

What it does *not* pin is prose. The sample's explanatory lines and the library's refusal
sentences are free to be reworded; the assertions below key on names and values that are API.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The three functions `host_tools.py` stamps, all of which must register.
_REGISTERED = ("semver_bump", "fetch_changelog", "publish_release_note")

#: The fourth, deliberately unstamped. `require_declared=True` must turn it away — and the
#: registered line must not name it, which is the half that would go unnoticed if the refusal
#: printed but the registration happened anyway.
_UNSTAMPED = "rerun_failed_jobs"

#: `HostToolAggregate`, leg by leg, as this registry folds to. Every value here is a fold the
#: library performs rather than something the sample chose, so each is a real assertion about
#: published behaviour: the weakest source tier wins, one USER tool gates the whole surface,
#: and the gate above kept `has_undeclared` down.
_AGGREGATE = {
    "result_integrity": "untrusted",
    "requires_approval": "True",
    "has_undeclared": "False",
}

#: Both refusals, by exception class name — which is API, where the sentences are not. The
#: sample prints them with `type(refusal).__name__`, so these appear only if the router really
#: raised and the sample really caught it.
_REFUSALS = ("SandboxCapabilityDenied", "SandboxIdentityDenied")

#: The narrowed fold: act 4 rebuilds the registry without the USER tool and reports what it
#: comes to. Half of this is covered by the completion line already — the sample calls
#: `ensure_can_serve` on the narrowed spec unguarded, so a router that refused would end the
#: run before it printed one — and that is the half worth having twice, because it is the
#: claim `least privilege is what a host registers` rests on.
_NARROWED = ("identities={app}", "requires_approval=False")

#: The last line: four acts, and no sandbox. Every other sample's check asserts a sandbox *was*
#: created; this one asserts none ever was, because the whole point is that the router answers
#: at attach. A truncated run has no such line at all, which both readings catch.
_COMPLETED = re.compile(r"Completed\s+(\d+)\s+of\s+4\s+acts\.\s+Acquired\s+(\d+)\s+sandbox", re.I)


def _line_containing(output: str, needle: str) -> str | None:
    """The first line holding ``needle``, or ``None``. Line-scoped so a name found in the
    sample's prose is not read as a name found on the line that lists what registered."""
    for line in output.splitlines():
        if needle in line:
            return line
    return None


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    registered = _line_containing(output, "registered:")
    if registered is None:
        failures.append("no 'registered:' line — act 1 did not run")
    else:
        for name in _REGISTERED:
            if name not in registered:
                failures.append(
                    f"host tool {name!r} is not on the 'registered:' line — a stamped tool the "
                    "registry should have accepted did not register"
                )
        if _UNSTAMPED in registered:
            failures.append(
                f"{_UNSTAMPED!r} is on the 'registered:' line — it carries no declaration and "
                "require_declared=True must refuse it"
            )

    refused = _line_containing(output, "refused:")
    if refused is None or _UNSTAMPED not in refused:
        failures.append(
            f"no 'refused:' line naming {_UNSTAMPED!r} — the require_declared gate did not fire"
        )

    for leg, expected in _AGGREGATE.items():
        line = _line_containing(output, f"{leg}:")
        if line is None:
            failures.append(f"the aggregate's {leg!r} was not reported — act 2 did not run")
        elif expected not in line:
            failures.append(
                f"the aggregate reports {leg!r} as {line.split(':', 1)[1].strip()!r}, expected "
                f"{expected!r} — the fold this registry produces changed"
            )

    caps = _line_containing(output, "outbound_caps:")
    if caps is None or "public" not in caps:
        failures.append(
            "'public' is not among outbound_caps — the sink cap is carried verbatim and "
            "unfolded, so it must arrive exactly as declared"
        )

    identities = _line_containing(output, "identities:")
    if identities is None or not ("app" in identities and "user" in identities):
        failures.append(
            "the aggregate's identities are not {app, user} — both a declared APP tool and a "
            "declared USER tool are registered, so both must appear"
        )

    if _line_containing(output, "sealed:") is None:
        failures.append(
            "no 'sealed:' line — registering after the aggregate was taken was not refused, so "
            "the surface can still widen under a policy already derived from it"
        )

    if "ensure_can_serve" not in output:
        failures.append("act 3 did not report ensure_can_serve — the permitted path did not run")

    for exception in _REFUSALS:
        if exception not in output:
            failures.append(
                f"{exception} is not in the output — that deny axis did not refuse the spec"
            )

    narrowed = _line_containing(output, "folds to identities=")
    if narrowed is None:
        failures.append(
            "act 4 did not report the narrowed registry — the way past the identity refusal is "
            "a smaller surface registered from the start, and the sample runs it rather than "
            "describing it"
        )
    else:
        for fragment in _NARROWED:
            if fragment not in narrowed:
                failures.append(
                    f"the narrowed registry does not report {fragment!r} — dropping the only "
                    "USER tool must take the whole surface off approval-gated"
                )

    completed = _COMPLETED.search(output)
    if completed is None:
        failures.append(
            "no 'Completed N of 4 acts. Acquired N sandbox(es).' line — the sample did not run "
            "to completion"
        )
        return failures
    if int(completed.group(1)) != 4:
        failures.append(
            f"only {completed.group(1)} of 4 acts completed — the sample stopped part-way"
        )
    if int(completed.group(2)) != 0:
        failures.append(
            f"{completed.group(2)} sandbox(es) were acquired — this sample's claim is that every "
            "decision it shows is answered at attach, before a backend is ever asked for one"
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
        print(
            "FAIL: the host-tools sample did not exercise the published contract:", file=sys.stderr
        )
        for reason in failures:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    print("OK  the host-tools contract registered, folded, sealed and refused on both axes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
