"""Open or notify a workflow's unresolved failure tracker for this Actions run."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tomllib
from pathlib import Path


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
            if "pull_request" not in issue and args.marker in (issue.get("body") or "")
        ),
        None,
    )
    failure = f"{args.title}: [run and logs]({run_url})."
    if args.context:
        failure += f"\n\n{args.context}"
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
        "**Additional context**\nFurther failures comment here while this issue is open. Investigate and close "
        "after verifying recovery; a successful run does not automatically close it.\n"
    )
    gh(endpoint, body={"title": args.title, "body": body})


if __name__ == "__main__":
    main()
