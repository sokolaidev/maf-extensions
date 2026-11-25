"""Share one maintainer-owned issue across image security reporters."""

from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any

STATE = re.compile(r"<!-- image-security-issue-state\n(.*?)\n-->", re.DOTALL)


def marker(profile: str) -> str:
    """Validate the stable profile identity before including it in issue metadata."""
    if not re.fullmatch(r"[a-z0-9-]+", profile):
        raise ValueError("Invalid image profile")
    return f"<!-- image-security-issue:{profile} -->"


def matches(issues: list[dict], profile: str) -> list[dict]:
    """Only adopt trackers owned by the reporting bot, including dismissed trackers."""
    markers = (
        marker(profile),
        f"<!-- image-remediation-attention:{profile} -->",
        f"<!-- image-release-needed:{profile} -->",
    )
    return [
        issue
        for issue in issues
        if "pull_request" not in issue
        and issue.get("user", {}).get("login") == "github-actions[bot]"
        and any(value in (issue.get("body") or "") for value in markers)
    ]


def select(issues: list[dict], profile: str) -> dict | None:
    """Prefer the canonical issue, then an open maintainer tracker with its existing notes."""
    candidates = matches(issues, profile)
    return min(
        candidates,
        key=lambda issue: (
            marker(profile) not in (issue.get("body") or ""),
            issue.get("state", "open") != "open",
            f"<!-- image-remediation-attention:{profile} -->" not in (issue.get("body") or ""),
            -issue["number"],
        ),
        default=None,
    )


def findings_signals(findings: list[dict]) -> set[str]:
    """Version drift and disappearing findings are not new remediation opportunities."""
    result = set()
    for finding in findings:
        identifier = finding["id"]
        result.add(f"finding:{identifier}")
        if finding.get("severity") == "Critical":
            result.add(f"critical:{identifier}")
        for version in (finding.get("fix") or {}).get("versions") or []:
            result.add(f"fix:{identifier}:{version}")
    return result


def lanes(issue: dict | None) -> dict[str, Any]:
    """Read independent reporter state without inferring recovery from human notes."""
    if issue is None:
        return {}
    match = STATE.search(issue.get("body") or "")
    return json.loads(match[1]) if match else {}


def notice(issue: dict | None, signals: set[str]) -> str | None:
    """Notify only about added findings or remediation opportunities, never refreshed evidence."""
    saved = lanes(issue)
    if not saved or issue is None or issue.get("state", "open") != "open":
        return None
    known = {signal for value in saved.values() for signal in value["signals"]}
    added = sorted(signals - known)
    if not added:
        return None
    generation = sum(value.get("generation", 0) for value in saved.values())
    digest = hashlib.sha256(json.dumps([generation, added]).encode()).hexdigest()
    details = html.escape(
        "\n".join(
            "Chrome security advisory changed; see the Chrome evidence section."
            if value.startswith("advisory:chrome:")
            else value
            for value in added
        )
    ).replace("@", "&#64;")[:8000]
    return f"<!-- image-security-change:{digest} -->\nNew findings or remediation opportunities:\n\n<pre>{details}</pre>\n\nThe issue body retains the latest evidence from each reporter."


def already_notified(comments: list[dict], message: str) -> bool:
    """Recognize an acknowledged notification when a preceding write response was lost."""
    marker = message.split("\n", 1)[0]
    return any(
        comment.get("user", {}).get("login") == "github-actions[bot]"
        and marker in (comment.get("body") or "")
        for comment in comments
    )


def remember(saved: dict[str, Any], lane: str, signals: set[str], active: bool) -> None:
    """Advance receipt identity on state changes, including removals, but not evidence refreshes."""
    previous = saved.get(lane, {})
    state = {"signals": sorted(signals), "active": active}
    changed = any(previous.get(key) != value for key, value in state.items())
    saved[lane] = {**state, "generation": previous.get("generation", 0) + int(changed)}


def update(
    issue: dict | None,
    profile: str,
    lane: str,
    content: str,
    signals: set[str],
    *,
    active: bool = True,
    related: list[dict] | None = None,
) -> dict[str, str] | None:
    """Refresh evidence quietly; reopen the same dismissed issue only for new actionable facts."""
    saved = lanes(issue)
    old_body = (issue or {}).get("body") or ""
    recovered = bool(saved) and not any(value["active"] for value in saved.values())
    legacy_active = any(
        value.get("state", "open") == "open"
        and f"<!-- image-remediation-attention:{profile} -->" in (value.get("body") or "")
        and not lanes(value)
        for value in ([issue] if issue else []) + (related or [])
    )
    if legacy_active:
        remember(saved, "legacy-remediation", set(), True)
    known = {signal for value in saved.values() for signal in value["signals"]}
    # Legacy trackers lack structured alert state. Preserve their dismissal on adoption.
    novel = bool(saved and signals - known)
    remember(saved, lane, signals, active)
    start, end = f"<!-- image-issue-section:{lane} -->", f"<!-- /image-issue-section:{lane} -->"
    section = f"{start}\n{content}\n{end}"
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    body = STATE.sub("", old_body).strip()
    if "<!-- image-release-state\n" in body and "<!-- image-issue-section:release -->" not in body:
        if lanes(issue):
            raise ValueError("Shared image issue has no release section boundary")
        body = (
            f"<!-- image-issue-section:release -->\n{body}\n<!-- /image-issue-section:release -->"
        )
    if pattern.search(body):
        body = pattern.sub(lambda _: section, body)
    else:
        body = f"{body}\n\n{section}".strip()
    if marker(profile) not in body:
        body = f"{marker(profile)}\n\n{body}"
    for previous in related or []:
        if issue is not None and previous["number"] == issue["number"]:
            continue
        reference = (
            f"Earlier report: #{previous['number']} (retained evidence and maintainer discussion)."
        )
        if reference not in body:
            body += f"\n\n{reference}"
    encoded = json.dumps(saved, sort_keys=True).replace("<", "\\u003c").replace(">", "\\u003e")
    body += f"\n\n<!-- image-security-issue-state\n{encoded}\n-->"
    if len(body) > 60_000:
        raise ValueError("Image issue exceeds the issue size limit")
    payload = {"title": f"Image security needs attention: {profile}", "body": body}
    if issue is not None:
        pending = any(value["active"] for value in saved.values())
        if not pending and lane == "release" and issue.get("state") != "closed":
            payload.update(state="closed", state_reason="completed")
        elif issue.get("state") == "closed" and (
            legacy_active or (active and (novel or recovered))
        ):
            payload.update(state="open")
        if all(issue.get(key) == value for key, value in payload.items()):
            return None
    return payload


def retirement(issue: dict, canonical: dict, profile: str) -> dict[str, str] | None:
    """Keep the original evidence and discussion reachable from the canonical tracker."""
    if issue["number"] == canonical["number"] or issue.get("state") == "closed":
        return None
    return {
        "body": (issue.get("body") or "")
        + f"\n\nTracking continues in #{canonical['number']} for image `{profile}`. This closure consolidates reports; it does not establish security recovery.",
        "state": "closed",
        "state_reason": "not_planned",
    }
