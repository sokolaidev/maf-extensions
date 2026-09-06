"""SARIF in, readable diagnostics out.

Pure functions over the Bicep CLI's ``--diagnostics-format sarif`` output.  No sandbox, no
Azure, no I/O — which is why the parser is the one part of this workload that is trivially
testable and has been since the first commit.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

__all__ = ["RESTORE_FAILURE_RULES", "count_restore_failures", "format_diagnostics", "parse_sarif"]

# Maximum characters from a SARIF blob fed into the parser.
_SARIF_MAX_CHARS = 200_000

#: Diagnostics that mean a module artifact never arrived: BCP190 (artifact not restored),
#: BCP191 (restore failed), BCP192 (restore failed, with the transport's reason).  When any
#: of these is present the compiler never loaded the module's types, so every check on that
#: module's inputs and outputs silently did not run — the run's other diagnostics describe a
#: DIFFERENT program than the one that would deploy.  Callers must surface that as "the
#: validation is incomplete", never fold it into an ordinary diagnostic count — a reviewer
#: once read exactly that soup, discounted it as environment noise, and PASSed files that do
#: not compile.
RESTORE_FAILURE_RULES = frozenset({"BCP190", "BCP191", "BCP192"})


def count_restore_failures(diagnostics: list[dict[str, Any]]) -> int:
    """How many of these diagnostics are module-restore failures."""
    return sum(1 for d in diagnostics if d.get("rule") in RESTORE_FAILURE_RULES)


def parse_sarif(text: str) -> list[dict[str, Any]] | None:
    """Parse a SARIF JSON blob into a compact list of diagnostic dicts.

    Returns ``None`` on any parse failure — the caller must treat that as an error rather
    than as zero diagnostics, or a broken sandbox reads as a clean build.
    """
    try:
        data = json.loads(text[:_SARIF_MAX_CHARS])
    except (json.JSONDecodeError, ValueError):
        return None

    try:
        return _diagnostics(data)
    except (AttributeError, TypeError):
        # Parsing as JSON says nothing about the shape, and every lookup below assumes it —
        # a `null` where an object belongs, or a top-level array, reaches a `.get` that is not
        # there. That has to arrive as the documented `None`, which the caller renders and
        # logs: raised instead, it leaves the tool with no result at all to return.
        return None


def _diagnostics(data: Any) -> list[dict[str, Any]]:
    """The SARIF walk itself, over a blob that has parsed but is not yet known to be SARIF."""
    diagnostics: list[dict[str, Any]] = []
    for run in data.get("runs", []):
        rules: dict[str, Any] = {}
        for rule in run.get("tool", {}).get("driver", {}).get("rules", []):
            rules[rule.get("id", "")] = rule

        for result in run.get("results", []):
            rule_id = result.get("ruleId", "")
            rule = rules.get(rule_id, {})
            message = result.get("message", {}).get("text", "")
            level = result.get("level", "warning")
            locs: list[dict[str, Any]] = []
            for loc in result.get("locations", []):
                region = loc.get("physicalLocation", {}).get("region", {})
                artifact = (
                    loc.get("physicalLocation", {}).get("artifactLocation", {}).get("uri", "")
                )
                locs.append(
                    {
                        "file": artifact,
                        "line": region.get("startLine"),
                        "column": region.get("startColumn"),
                    }
                )
            diagnostics.append(
                {
                    "rule": rule_id,
                    "level": level,
                    "message": message,
                    "locations": locs,
                    "help": rule.get("helpUri", ""),
                }
            )
    return diagnostics


#: What a location renders as when :func:`_renamed` could not identify it.
_UNIDENTIFIED = "an unidentified file"


def _renamed(location: str, absolute: str, rename: Mapping[str, str] | None) -> str:
    """``location`` as it may be shown, given the caller's map of every file it wrote.

    Two matches, and only one of them attributes.

    **Exact** — against the stripped location or the raw absolute path — identifies the file, so
    the caller's rendering for it is used as given, request position and all.  ``absolute`` is
    what makes this the ordinary case rather than the lucky one: Bicep is handed the path this
    call wrote and reports it back, so the caller's own ``sandbox_path`` matches it whether or
    not ``strip_prefix`` succeeded.

    **Trailing component** — a fallback for a location this run did not strip, which still ends
    in the file's name.  It cannot identify anything: a written ``main.bicep`` and an unrelated
    ``/vendor/main.bicep`` match the same key equally well, and picking a longest or first match
    only chooses between guesses.  So it renders :data:`_UNIDENTIFIED` and claims no position.
    The name is still withheld, because a location that ends in a written file's name may *be*
    that file, and that file's name may be content the framework hid.

    Either way the *entire* location is replaced rather than the matched part: half of a path
    that contained the name is still the name, and the directories around it are the sandbox's
    internal layout, which ``format_diagnostics`` does not put in front of the model either.
    """
    if not rename or not location:
        return location
    for candidate in (location, absolute):
        if candidate and candidate in rename:
            return rename[candidate]
    if any(location.endswith("/" + real) for real in rename):
        return _UNIDENTIFIED
    return location


def format_diagnostics(
    diagnostics: list[dict[str, Any]],
    phase: str,
    *,
    strip_prefix: str | None = None,
    rename: Mapping[str, str] | None = None,
) -> str:
    """Render a compact human-readable summary of SARIF diagnostics.

    ``strip_prefix`` is the in-sandbox directory the sources were written to.  Real Bicep
    reports locations as absolute ``file://`` URIs, so without it every diagnostic reads
    ``file:///maf-sandbox/work/8f2c1d/main.bicep`` — which puts the sandbox's internal layout into
    the model's context, and gives the *same* file a different path on every round because
    the directory is per-call.  Stripped, it reads ``main.bicep``: the name the agent used.

    ``rename`` maps a location to what may be shown in its place, and exists because stripping
    the directory is not the same as making the name safe.  A name the framework expanded out of
    hidden content reaches here having matched the caller's listing, and the compiler then
    reports diagnostics *against* it — so a location renders the hidden value on the ordinary
    path where the file simply has an error in it.

    A caller passes **every file it wrote**, mapping the ones it may echo to themselves, and
    under every spelling the compiler might use for them — including the absolute path it was
    given, which is what a real diagnostic reports and what makes the match exact rather than
    inferred.  Not only the unshowable ones: a map of those alone cannot tell a diagnostic about
    a visible file from one about a hidden file that shares its basename.  A location matching
    nothing is shown as the compiler reported it, because a file the caller never wrote is one
    it cannot vouch for either way.
    """
    if not diagnostics:
        return f"{phase}: no diagnostics"
    lines = [f"{phase}: {len(diagnostics)} diagnostic(s)"]
    for d in diagnostics:
        loc_parts: list[str] = []
        for loc in d.get("locations", []):
            raw = loc.get("file", "")
            f = _renamed(_relative_location(raw, strip_prefix), raw.removeprefix("file://"), rename)
            ln = loc.get("line")
            col = loc.get("column")
            if not f:
                continue
            if ln and col:
                loc_parts.append(f"{f}:{ln}:{col}")
            elif ln:
                # Bicep emits `charOffset` rather than `startColumn`, so there is usually no
                # column to show. Printing the line alone beats printing "None".
                loc_parts.append(f"{f}:{ln}")
            else:
                loc_parts.append(f)
        loc = ", ".join(loc_parts) if loc_parts else "—"
        lines.append(
            f"  [{d.get('level', '?')}] {d.get('rule', '')} @ {loc}: {d.get('message', '')}"
        )
    return "\n".join(lines)


def _relative_location(uri: str, strip_prefix: str | None) -> str:
    """Turn a SARIF artifact URI back into the path the caller asked about."""
    if not uri:
        return ""
    path = uri.removeprefix("file://")
    if strip_prefix:
        path = path.removeprefix(strip_prefix.rstrip("/") + "/")
    return path
