"""Say where a maintainer will see it that a merged Release PR was never released.

    <state.json> | python scripts/report_stuck_releases.py

One JSON document arrives on stdin — when the run started, the merged Release PRs still
labelled ``autorelease: pending``, and the tracking issue if one is already open. A JSON plan
goes out on stdout: ``open``, ``update``, ``close`` or ``none``, with the text to post.

release-please releases nothing behind a merged Release PR it could not finish. It takes the
oldest unfinished one and stops there, whatever commit triggered the run, so one refused
release stops every package's — and the only trace is a red run on ``main`` that nobody opens.
The tracking issue is the signal that outlives the run.

Nothing here reaches the network. The caller gathers the state and carries out the plan; this
decides and renders, which is what `tests/test_report_stuck_releases.py` holds it to.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from typing import Any

#: Hidden in the tracking issue's body. Identity is the marker rather than the title, so an
#: issue somebody retitled is still found and updated instead of duplicated.
MARKER = "<!-- stuck-release-tracker -->"

#: Constant on purpose: the issue is edited in place, and a title that moved with its contents
#: would rename an issue people are already reading.
TITLE = "A merged Release PR was never released, so no package can release"

#: `chore(main): release maf-sandbox-acas 0.13.0` — release-please's title for a
#: single-package Release PR, which is the only shape this repository produces, because
#: `release-please-config.json` sets `separate-pull-requests`.
#:
#: Both halves are held to what may appear in a git tag, because the tag built from them is
#: rendered into shell commands a maintainer copies and runs. A title carrying anything else
#: names no tag at all, and the issue says so rather than rendering a command from it.
_VERSION = r"\d[0-9A-Za-z.+-]*"
_RELEASE_TITLE = re.compile(
    rf"\brelease\s+(?P<package>[A-Za-z0-9._-]+)\s+v?(?P<version>{_VERSION})\s*$"
)

#: Far enough back that a pull request whose merge time GitHub did not report is reported
#: rather than filtered away. Missing the wedge is the failure this exists to prevent, and a
#: false alarm closes itself on the next run.
_LONG_AGO = datetime(1970, 1, 1, tzinfo=UTC)

#: The tracking issue's fixed paragraphs, named rather than wrapped inside the list that
#: renders it: adjacent string literals in a list cannot be told from a missing comma.
_WHAT_IS_STUCK = (
    "**Release Please ran and left a merged Release PR unreleased.** It takes the oldest "
    "unfinished one and stops there, whatever commit triggered the run, so until this is "
    "cleared **no package can release** — not core, not any backend."
)
_HOW_TO_FINISH = (
    "**The label flip is the step nobody guesses.** Without it release-please retries the "
    "same release for ever, and the train stays stuck even once the tag exists. Then "
    "dispatch Release Please (`gh workflow run release-please.yml`) so the rest of the "
    "train drains, and check that the publish reached PyPI — a tag created by a user "
    "token starts no workflow, which is why the publish is dispatched above."
)
_WHO_OWNS_THIS_ISSUE = (
    "This issue is opened, edited and closed by `release-please.yml`. It closes itself on "
    "the first run that finds nothing stuck, so leave it open until the release lands."
)


def tag_for(title: str) -> str | None:
    """The tag release-please would have created for a Release PR titled ``title``."""
    match = _RELEASE_TITLE.search(title)
    if match is None:
        return None
    return f"{match['package']}-v{match['version']}"


def _moment(text: str | None) -> datetime:
    """A GitHub timestamp as an aware datetime, or the far past when it says nothing."""
    if not text:
        return _LONG_AGO
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return _LONG_AGO


def stuck_releases(pending: list[dict[str, Any]], run_started_at: str) -> list[dict[str, Any]]:
    """The pending Release PRs this run should have released, oldest merge first.

    A Release PR merged *while* the run was in flight belongs to the run its own merge
    triggered, which has not had its turn yet — the workflow's concurrency group serialises
    them. Reporting one of those would be a false alarm that closes itself a minute later.
    """
    started = _moment(run_started_at)
    return sorted(
        (pr for pr in pending if _moment(pr.get("mergedAt")) <= started),
        key=lambda pr: _moment(pr.get("mergedAt")),
    )


def _recovery(pr: dict[str, Any], released_tags: list[str]) -> list[str]:
    """The commands that release one stuck Release PR by hand, carrying its own values.

    Which commands depends on whether its Release exists: release-please creates that first
    and flips the label after, so a failure in between leaves both true at once.
    """
    title = str(pr.get("title", ""))
    tag = tag_for(title)
    if tag is None:
        unreadable = (
            f"`{title}` names no package and version, so the tag it owes cannot be read off "
            "it. Take that from the manifest entry the pull request bumped."
        )
        return [unreadable]
    sha = str((pr.get("mergeCommit") or {}).get("oid") or "") or "<merge commit>"
    package, version = tag.rsplit("-v", 1)
    finish = [
        f'gh pr edit {pr.get("number")} --remove-label "autorelease: pending" \\',
        '  --add-label "autorelease: tagged"',
        f"gh workflow run publish-packages.yml --ref {tag} \\",
        f"  -f package={package} -f target=pypi",
        "```",
    ]
    if tag in released_tags:
        already = (
            f"**`{tag}` already exists**, so release-please created the Release and stopped "
            f"after it — what was refused is the labelling call, not the release. Creating it "
            f"again would be rejected as a duplicate. Only the flip and the publish are left."
        )
        return [already, "", "```bash", *finish]
    lead = (
        f"Its tag is `{tag}`, at `{sha}`. The notes are that version's section of "
        f"`packages/{package}/CHANGELOG.md`, which this pull request wrote."
    )
    return [
        lead,
        "",
        "```bash",
        f"gh release create {tag} --target {sha} \\",
        f"  --title '{package} {version}' --notes-file notes.md",
        *finish,
    ]


def body(stuck: list[dict[str, Any]], run_url: str, released_tags: list[str]) -> str:
    """The tracking issue's body: what is stuck, what it costs, and how to clear it."""
    lines = [
        MARKER,
        "",
        _WHAT_IS_STUCK,
        "",
        "| Release PR | Merged | Tag it owes |",
        "| --- | --- | --- |",
    ]
    for pr in stuck:
        tag = tag_for(str(pr.get("title", "")))
        owes = "unknown" if tag is None else f"`{tag}`"
        if tag is not None and tag in released_tags:
            owes = f"`{tag}` — already created"
        lines.append(
            f"| [#{pr.get('number')}]({pr.get('url', '')}) {pr.get('title', '')} "
            f"| {pr.get('mergedAt') or 'unknown'} | {owes} |"
        )
    if run_url:
        lines += ["", f"Noticed by {run_url}."]
    lines += ["", "## Clearing it by hand", ""]
    for pr in stuck:
        lines += [f"### #{pr.get('number')}", "", *_recovery(pr, released_tags), ""]
    lines += [
        _HOW_TO_FINISH,
        "",
        "`docs/maintainers.md` carries the same steps with the reasoning behind each.",
        "",
        _WHO_OWNS_THIS_ISSUE,
    ]
    return "\n".join(lines)


def _summary(action: str, stuck: list[dict[str, Any]]) -> str:
    """What the step writes to the run summary, whichever way it went."""
    if not stuck:
        if action == "close":
            return "the stuck release cleared; closing the tracking issue"
        return "no merged Release PR is waiting to be released"
    listed = ", ".join(f"#{pr.get('number')}" for pr in stuck)
    return (
        f"**no package can release**: {listed} merged and was never released. Tracking issue "
        f"{'opened' if action == 'open' else 'updated'}."
    )


def plan(document: dict[str, Any]) -> dict[str, Any]:
    """What to do about the state in ``document`` — open, update, close, or nothing."""
    stuck = stuck_releases(document.get("pending") or [], str(document.get("run_started_at", "")))
    issue = document.get("issue")
    number = issue.get("number") if isinstance(issue, dict) else None
    if stuck:
        action = "update" if number is not None else "open"
        return {
            "action": action,
            "issue": number,
            "title": TITLE,
            "body": body(
                stuck, str(document.get("run_url", "")), document.get("released_tags") or []
            ),
            "summary": _summary(action, stuck),
        }
    if number is not None:
        return {
            "action": "close",
            "issue": number,
            "comment": (
                "Every merged Release PR has been released, so the train is moving again. "
                "Closed by `release-please.yml`, which opens a new tracker if it happens again."
            ),
            "summary": _summary("close", stuck),
        }
    return {"action": "none", "summary": _summary("none", stuck)}


def main(argv: list[str]) -> int:
    """Print the marker, or read the state on stdin and print the plan."""
    if argv[1:] == ["--marker"]:
        print(MARKER)
        return 0
    if argv[1:]:
        print(f"usage: {argv[0]} [--marker] < state.json", file=sys.stderr)
        return 2
    print(json.dumps(plan(json.load(sys.stdin)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
