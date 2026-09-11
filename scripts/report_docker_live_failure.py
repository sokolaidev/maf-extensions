"""Open or notify the unresolved Docker live failure tracker for this Actions run."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

MARKER = "<!-- docker-live-failure-tracker -->"
TITLE = "Docker live checks failed"


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


def main() -> None:
    """Report the failed run identified by the standard GitHub Actions environment."""
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
            if "pull_request" not in issue and MARKER in (issue.get("body") or "")
        ),
        None,
    )
    failure = f"Docker live checks failed: [run and logs]({run_url})."
    if tracker is not None:
        gh(f"{endpoint}/{tracker['number']}/comments", body={"body": failure})
        return

    root = Path(__file__).resolve().parent.parent
    packages = []
    for name in (
        "maf-sandbox",
        "maf-sandbox-docker",
        "maf-sandbox-codeact",
        "maf-sandbox-bicep",
        "maf-sandbox-deepagents",
    ):
        project = tomllib.loads(
            (root / "packages" / name / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        packages.append(f"{name} {project['version']}")
    body = (
        f"{MARKER}\n\n**Describe the bug**\n{failure}\n\n"
        "**To Reproduce**\nRe-run the linked attempt, or dispatch `docker-live.yml` "
        "against the failing commit. Inspect the first failed step for setup, registry, "
        "daemon, or test failures.\n\n"
        "**Expected behavior**\nAll Docker live checks pass.\n\n"
        f"**Environment**\n- Runner: ubuntu-latest\n- Packages: {', '.join(packages)}"
        f" (workspace commit {os.environ['GITHUB_SHA']})\n"
        "- Python version: see the linked run's uv setup and pytest logs; "
        "setup may fail before Python starts.\n\n"
        "**Additional context**\nLive checks run after merge, daily, and on manual dispatch. "
        "Further failures comment here while this issue is open. Investigate and close "
        "after verifying recovery; a successful run does not automatically close it.\n"
    )
    gh(endpoint, body={"title": TITLE, "body": body})


if __name__ == "__main__":
    main()
