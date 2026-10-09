"""Close the image security tracker only after a complete default-branch scan."""

from __future__ import annotations

import json
import os
from datetime import datetime

from report_workflow_failure import gh
from select_image_security import PROFILES

MARKER = "<!-- image-security-failure-tracker -->"


def main() -> None:
    """Verify this attempt's coverage before recording recovery on an older tracker."""
    if (
        os.environ["GITHUB_EVENT_NAME"] not in {"schedule", "workflow_dispatch"}
        or os.environ["GITHUB_REF"] != f"refs/heads/{os.environ['DEFAULT_BRANCH']}"
        or os.environ["SELECTED_PROFILE"] != "all"
    ):
        return

    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    endpoint = f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"
    run = json.loads(gh(endpoint))
    started = datetime.fromisoformat(run["run_started_at"])
    pages = json.loads(gh(f"{endpoint}/jobs?per_page=100", "--paginate", "--slurp"))
    jobs = {job["name"]: job for page in pages for job in page["jobs"]}
    required = {f"Image security ({profile})" for profile in PROFILES}
    for name in required | {"Select image security profiles", "Image security checks"}:
        job = jobs.get(name)
        if job is None or job["conclusion"] != "success":
            print(f"Recovery deferred: {name} did not complete successfully in this attempt.")
            return
        if name in required and not any(
            step["name"] == "Scan the retained inventory" and step["conclusion"] == "success"
            for step in job["steps"]
        ):
            print(f"Recovery deferred: {name} has no completed scan in this attempt.")
            return

    head = json.loads(gh(f"repos/{repo}/commits/{os.environ['DEFAULT_BRANCH']}"))
    if run["head_sha"] != head["sha"]:
        print("Recovery deferred: the default branch has advanced beyond this scan.")
        return

    issues = f"repos/{repo}/issues"
    pages = json.loads(gh(f"{issues}?state=open&per_page=100", "--paginate", "--slurp"))
    run_url = f"{os.environ['GITHUB_SERVER_URL']}/{repo}/actions/runs/{run_id}/attempts/{attempt}"
    for page in pages:
        for issue in page:
            if (
                "pull_request" in issue
                or MARKER not in (issue.get("body") or "")
                or issue["user"]["login"] != "github-actions[bot]"
                or datetime.fromisoformat(issue["updated_at"]) >= started
            ):
                continue
            tracker = f"{issues}/{issue['number']}"
            gh(
                f"{tracker}/comments",
                body={
                    "body": (
                        f"Recovered in [this full-profile run]({run_url}). "
                        f"All {len(PROFILES)} profiles completed their inventory scans and "
                        "passed the High/Critical gate, including unfixed findings. "
                        "Closing this workflow failure tracker automatically."
                    )
                },
            )
            gh(tracker, "--method", "PATCH", body={"state": "closed", "state_reason": "completed"})


if __name__ == "__main__":
    main()
