"""Assert that a live `samples/10_inprocess_host_tools` run exercised the real contract.

The live workflow installs the *published* wheels, runs the sample and pipes its output here.

    python samples/10_inprocess_host_tools/agent.py | tee out.txt
    python scripts/check_live_host_tools_sample.py out.txt   # or: ... | python …

Alone in this set it matches *strictly*, because no model stands between the library and
stdout: the printed values are `maf-sandbox`'s own answers, so a mismatch is a behaviour
change rather than a paraphrase. Strictly means **equal, not contains** — sets are parsed and
compared whole, scalars as tokens. Widening is the failure this exists to notice, and a
membership test cannot see it. Prose is not pinned: every assertion keys on a name or a value
that is API.

Exits non-zero listing every reason it failed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: The whole registered surface, compared as a set. Three stamped functions register and the
#: fourth must not, so an extra name is as much a failure as a missing one — a published
#: surface that grew a tool is exactly what a host would want a job to catch.
_REGISTERED = frozenset({"semver_bump", "fetch_changelog", "publish_release_note"})

#: The fourth, deliberately unstamped. `require_declared=True` must turn it away, and it must
#: not appear in the set above — the half that would go unnoticed if the refusal printed but
#: the registration happened anyway.
_UNSTAMPED = "rerun_failed_jobs"

#: `HostToolAggregate`'s scalar legs, as this registry folds to. Each is a fold the library
#: performs rather than a value the sample chose: the weakest source level wins, and one USER
#: tool gates the whole surface.
_AGGREGATE = {
    "result_integrity": "untrusted",
    "requires_approval": "True",
    "has_undeclared": "False",
}

#: The two set-valued legs. `outbound_caps` is carried verbatim and unfolded, so it must
#: arrive exactly as declared — a second cap appearing means the surface widened.
_OUTBOUND_CAPS = frozenset({"public"})
_IDENTITIES = frozenset({"app", "user"})

#: What act 4's second registry folds to once the only USER tool is left out. The claim behind
#: "least privilege is what a host registers": drop that tool and the surface comes off
#: approval-gated, which is what lets the same denied_identities router serve it.
_NARROWED_IDENTITIES = frozenset({"app"})
_NARROWED_APPROVAL = "False"

#: Both refusals, by exception class name — which is API, where the sentences are not. The
#: sample prints them with `type(refusal).__name__`, so these appear only if the router really
#: raised and the sample really caught it.
_REFUSALS = ("SandboxCapabilityDenied", "SandboxIdentityDenied")

#: The footer: how many acts ran, and how many sandboxes the backends were asked for. The
#: sample reads both back from real state — the acquisition count from the backends' own
#: `keys` — so this compares an observation rather than a literal it printed.
_FOOTER = re.compile(r"Completed\s+(\d+)\s+of\s+4\s+acts\.\s+Acquired\s+(\d+)\s+sandbox", re.I)


#: The labels the sample prints one line each for. `refused:` is not among them: it labels two
#: lines by design, and `assess` reads every one of them.
_LABELS = (
    "registered:",
    "result_integrity:",
    "outbound_caps:",
    "identities:",
    "requires_approval:",
    "has_undeclared:",
    "sealed:",
)


def _labelled_line(output: str, key: str) -> str | None:
    """The first line whose own label is ``key``, or ``None``.

    Labelled, not merely holding: `cannot be registered:` sits mid-sentence on the sealed line
    and `allowed_identities:` mid-sentence on a refusal, and both answered before this.
    """
    for line in output.splitlines():
        if line.strip().startswith(key):
            return line
    return None


def _line_containing(output: str, needle: str) -> str | None:
    """The first line holding ``needle`` anywhere, or ``None``.

    For what the sample prints mid-sentence, where there is no label to anchor on.
    """
    key = re.compile(rf"(?<!\w){re.escape(needle)}")
    for line in output.splitlines():
        if key.search(line):
            return line
    return None


def _assess_each_label_appears_once(output: str) -> list[str]:
    """A label on two lines is two answers, and taking the first would pick one to believe."""
    return [
        f"{key!r} labels {count} lines, so none of them can be trusted — the sample prints it once"
        for key in _LABELS
        if (count := sum(1 for line in output.splitlines() if line.strip().startswith(key))) > 1
    ]


def _after(line: str, key: str) -> str:
    """Whatever follows ``key`` on ``line``, stripped. ``key`` carries its own separator."""
    _, _, rest = line.partition(key)
    return rest.strip()


def _token(text: str) -> str:
    """The first whitespace-separated word of ``text``.

    What makes a scalar comparison exact while still allowing the trailing parenthetical the
    sample prints beside `has_undeclared`.
    """
    parts = text.split()
    return parts[0] if parts else ""


def _brace_set(text: str) -> frozenset[str]:
    """Parse ``{a, b}`` or ``{'a', 'b'}`` into a set of bare names — ``{}`` gives the empty set.

    Anything without a brace pair gives the empty set too, which compares unequal to every
    expectation here and so fails loudly rather than being read as a match.
    """
    if "{" not in text or "}" not in text:
        return frozenset()
    inner = text[text.index("{") + 1 : text.rindex("}")]
    return frozenset(part.strip().strip("'\"") for part in inner.split(",") if part.strip())


def _comma_set(text: str) -> frozenset[str]:
    """Parse ``a, b, c`` into a set of bare names."""
    return frozenset(part.strip() for part in text.split(",") if part.strip())


def assess(output: str) -> list[str]:
    """Return every reason ``output`` is not a healthy sample run — empty means it passed."""
    failures: list[str] = []

    registered = _labelled_line(output, "registered:")
    if registered is None:
        failures.append("no 'registered:' line — act 1 did not run")
    else:
        found = _comma_set(_after(registered, "registered:"))
        if found != _REGISTERED:
            missing = sorted(_REGISTERED - found)
            extra = sorted(found - _REGISTERED)
            detail = ", ".join(
                part
                for part in (
                    f"missing {missing}" if missing else "",
                    f"unexpected {extra}" if extra else "",
                )
                if part
            )
            failures.append(
                f"the registered surface is {sorted(found)}, expected {sorted(_REGISTERED)} "
                f"({detail}) — compared whole, because a surface that grew a tool is as much a "
                "change as one that lost a tool"
            )

    # Any refusal, not the first: the sample refuses an APP-only tool before this one.
    refusals = [line for line in output.splitlines() if "refused:" in line]
    if not any(_UNSTAMPED in line for line in refusals):
        failures.append(
            f"no 'refused:' line naming {_UNSTAMPED!r} — the require_declared gate did not fire"
        )

    for leg, expected in _AGGREGATE.items():
        line = _labelled_line(output, f"{leg}:")
        if line is None:
            failures.append(f"the aggregate's {leg!r} was not reported — act 2 did not run")
            continue
        actual = _token(_after(line, f"{leg}:"))
        if actual != expected:
            failures.append(
                f"the aggregate reports {leg} as {actual!r}, expected exactly {expected!r} — "
                "the fold this registry produces changed"
            )

    caps = _labelled_line(output, "outbound_caps:")
    if caps is None:
        failures.append("the aggregate's outbound_caps was not reported — act 2 did not run")
    else:
        found_caps = _brace_set(_after(caps, "outbound_caps:"))
        if found_caps != _OUTBOUND_CAPS:
            failures.append(
                f"outbound_caps is {sorted(found_caps)}, expected exactly "
                f"{sorted(_OUTBOUND_CAPS)} — sink caps are carried verbatim and unfolded, so a "
                "cap appearing or disappearing is the surface changing"
            )

    identities = _labelled_line(output, "identities:")
    if identities is None:
        failures.append("the aggregate's identities were not reported — act 2 did not run")
    else:
        found_ids = _brace_set(_after(identities, "identities:"))
        if found_ids != _IDENTITIES:
            failures.append(
                f"the aggregate's identities are {sorted(found_ids)}, expected exactly "
                f"{sorted(_IDENTITIES)} — one declared APP tool and one declared USER tool are "
                "registered, and nothing else may appear"
            )

    if _labelled_line(output, "sealed:") is None:
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

    failures.extend(_assess_each_label_appears_once(output))
    failures.extend(_assess_narrowing(output))
    failures.extend(_assess_footer(output))
    return failures


def _assess_narrowing(output: str) -> list[str]:
    """Act 4's way out: the smaller surface, and what it folds to.

    Half of this is already covered — the sample calls ``ensure_can_serve`` on the narrowed
    spec unguarded, so a router that refused would end the run before the footer printed — and
    that is the half worth having twice, because it is the claim least privilege rests on.
    """
    line = _line_containing(output, "folds to identities=")
    if line is None:
        return [
            "act 4 did not report the narrowed registry — the way past the identity refusal is "
            "a smaller surface registered from the start, and the sample runs it rather than "
            "describing it"
        ]
    failures: list[str] = []
    narrowed = _brace_set(_after(line, "folds to identities="))
    if narrowed != _NARROWED_IDENTITIES:
        failures.append(
            f"the narrowed registry folds to identities {sorted(narrowed)}, expected exactly "
            f"{sorted(_NARROWED_IDENTITIES)} — leaving the only USER tool out must leave the "
            "app identity and nothing else"
        )
    # The sample prints this mid-sentence, so the token carries the clause's comma.
    approval = _token(_after(line, "requires_approval=")).rstrip(",")
    if approval != _NARROWED_APPROVAL:
        failures.append(
            f"the narrowed registry reports requires_approval={approval!r}, expected exactly "
            f"{_NARROWED_APPROVAL!r} — dropping the only USER tool must take the whole surface "
            "off approval-gated, which is what lets the same router serve it"
        )
    return failures


def _assess_footer(output: str) -> list[str]:
    """The two counts the sample reads back from real state."""
    footer = _FOOTER.search(output)
    if footer is None:
        return [
            "no 'Completed N of 4 acts. Acquired N sandbox(es).' line — the sample did not run "
            "to completion"
        ]
    failures: list[str] = []
    if int(footer.group(1)) != 4:
        failures.append(f"only {footer.group(1)} of 4 acts completed — the sample stopped part-way")
    if int(footer.group(2)) != 0:
        failures.append(
            f"{footer.group(2)} sandbox(es) were acquired — this sample's claim is that every "
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
