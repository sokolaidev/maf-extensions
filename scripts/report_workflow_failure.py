"""Open or notify a workflow's unresolved failure tracker for this Actions run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path


def notification_marker(value: str) -> str:
    """Ignore run links when comparing the state of successive monitor attempts."""
    return f"<!-- workflow-notification:{hashlib.sha256(value.encode()).hexdigest()} -->"


def monitor_notification() -> tuple[str, str, bool]:
    """Require all monitor stages and its retained health report before reporting recovery."""
    try:
        jobs = json.loads(os.environ.get("MONITOR_JOB_RESULTS", "{}"))
    except ValueError:
        jobs = {}
    if not isinstance(jobs, dict):
        jobs = {}
    failed = {
        name: jobs.get(name, "unknown")
        for name in ("begin", "scan", "finish", "result")
        if jobs.get(name) != "success" and not (name == "scan" and jobs.get(name) == "skipped")
    }
    raw = os.environ.get("MONITOR_NOTIFICATION", "")
    try:
        report = json.loads(raw) if raw else None
    except ValueError:
        report = None
    if not (
        isinstance(report, dict)
        and isinstance(report.get("attention"), bool)
        and isinstance(report.get("problems"), str)
        and isinstance(report.get("summary"), str)
    ):
        report = None
    recovered = not failed and report is not None and report["attention"] is False
    reasons = []
    if failed:
        reasons.append("Workflow stages: " + ", ".join(f"{k}={v}" for k, v in failed.items()) + ".")
    if report is None:
        reasons.append("The finish job did not deliver a health report; recovery is unverified.")
    else:
        reasons.append(report["summary"])
    state = json.dumps(
        {"jobs": failed, "health": report["problems"] if report else None, "recovered": recovered},
        sort_keys=True,
    )
    return "\n\n".join(reasons), notification_marker(state), recovered


def unchanged(tracker: dict, endpoint: str, marker: str) -> bool:
    """Compare only the latest bot notification, including one stored in the issue body."""
    pages = json.loads(
        gh(f"{endpoint}/{tracker['number']}/comments?per_page=100", "--paginate", "--slurp")
    )
    bodies = [tracker.get("body") or ""] + [
        comment.get("body") or ""
        for page in pages
        for comment in page
        if comment.get("user", {}).get("login") == "github-actions[bot]"
    ]
    for body in reversed(bodies):
        previous = re.search(r"<!-- workflow-notification:[a-f0-9]{64} -->", body)
        if previous:
            return previous.group() == marker
    return False


def gh(*args: str, body: dict[str, str] | None = None) -> str:
    """Call the GitHub API, using POST when a JSON body is supplied."""
    result = subprocess.run(
        ["gh", "api", *args, *(["--input", "-"] if body is not None else [])],
        input=json.dumps(body) if body is not None else None,
        stdout=subprocess.PIPE,
        text=True,
        check=True,
    )
    return result.stdout


def main(argv: list[str] | None = None) -> None:
    """Report the failed run identified by the standard GitHub Actions environment."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--marker", required=True, help="stable HTML comment identifying the tracker"
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--reproduce", required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--packages", nargs="+", required=True, help="workspace package names")
    parser.add_argument("--context", default="", help="guidance included on every failure")
    parser.add_argument(
        "--monitor", action="store_true", help="report monitor state changes and recovery"
    )
    parser.add_argument(
        "--recovery",
        default=(
            "Investigate and close after verifying recovery; "
            "a successful run does not automatically close it."
        ),
        help="recovery policy included in new trackers",
    )
    args = parser.parse_args(argv)

    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = (
        f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/"
        f"{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}"
    )
    endpoint = f"repos/{repo}/issues"
    pages = json.loads(gh(f"{endpoint}?state=open&per_page=100", "--paginate", "--slurp"))
    tracker = next(
        (
            issue
            for page in pages
            for issue in page
            if "pull_request" not in issue
            and issue.get("state", "open") == "open"
            and args.marker in (issue.get("body") or "")
        ),
        None,
    )
    failure = f"{args.title}: [run and logs]({run_url})."
    if args.context:
        failure += f"\n\n{args.context}"
    if args.monitor:
        details, marker, recovered = monitor_notification()
        if recovered:
            if tracker is None:
                return
            failure = (
                f"Monitoring recovered in [this run]({run_url}). "
                "All required stages succeeded and current image health and delivery passed. "
                "The tracker remains open for maintainer review."
            )
        failure += f"\n\n{details}\n\n{marker}"
        if tracker is not None and unchanged(tracker, endpoint, marker):
            print("Monitor state is unchanged; no notification needed.")
            return
    if tracker is not None:
        gh(f"{endpoint}/{tracker['number']}/comments", body={"body": failure})
        return

    root = Path(__file__).resolve().parent.parent
    packages = []
    for name in args.packages:
        project = tomllib.loads(
            (root / "packages" / name / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        packages.append(f"{name} {project['version']}")
    body = (
        f"{args.marker}\n\n**Describe the bug**\n{failure}\n\n"
        f"**To Reproduce**\n{args.reproduce}\n\n"
        f"**Expected behavior**\n{args.expected}\n\n"
        f"**Environment**\n- Runner: ubuntu-latest\n- Packages: {', '.join(packages)}"
        f" (workspace commit {os.environ['GITHUB_SHA']})\n"
        "- Python version: see the linked run's setup and execution logs; "
        "setup may fail before Python starts.\n\n"
        "**Additional context**\nFurther failures comment here while this issue is open. "
        f"{args.recovery}\n"
    )
    gh(endpoint, body={"title": args.title, "body": body})


if __name__ == "__main__":
    main()
