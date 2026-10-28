"""Explain the container monitor's final gate in its log and job summary."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

GUIDE = (
    "https://github.com/sokolaidev/maf-extensions/blob/main/"
    "docs/security/container-image-releases.md#responding-to-monitor-findings"
)


def main() -> int:
    """Keep assessment failures distinct from failed monitoring jobs."""
    problems = []
    for job, allowed in (
        ("BEGIN", {"success"}),
        ("SCAN", {"success", "skipped"}),
        ("FINISH", {"success"}),
    ):
        if os.environ.get(job) not in allowed:
            problems.append(f"The {job.lower()} job did not complete successfully.")
    attention = os.environ.get("ATTENTION")
    if attention != "false":
        problems.append(
            "Current images or monitoring delivery need attention."
            if attention == "true"
            else "The finish job did not provide a valid health result."
        )
    try:
        notification = json.loads(
            Path(os.environ["MONITOR_DIAGNOSTICS"]).read_text(encoding="utf-8")
        )
        details = notification["summary"]
        if not isinstance(details, str) or not details.strip():
            raise ValueError("Missing summary")
    except (OSError, ValueError, KeyError, TypeError):
        details = (
            "Image assessment details are unavailable; inspect the begin, scan and finish jobs."
        )
        problems.append("The monitor did not provide a readable assessment summary.")

    report = "## Container image monitor result\n\n" + details + "\n\n"
    if problems:
        report += "### Gate failures\n\n" + "\n".join(f"- {p}" for p in problems) + "\n\n"
        report += (
            "### Next actions\n\n"
            "- High/Critical findings: inspect the retained Grype report and SBOM; update the "
            "affected inputs, qualify a new image version and obtain publication approval. "
            "Do not overwrite existing tags or suppress findings.\n"
            "- Unavailable/stale evidence: inspect scan diagnostics and finish logs, repair "
            "the failing service or evidence delivery, then re-run all monitor jobs or "
            "dispatch from main. A failed-jobs-only retry cannot reuse the scan plan.\n"
            "- Pending publication delivery: reconcile the original release's completion "
            "evidence through its release/recovery workflow; a monitor retry cannot deliver it.\n\n"
        )
    else:
        report += "Current images and monitoring delivery passed the gate.\n\n"
    report += f"[Response guide]({GUIDE}). Historical findings alone do not fail this gate.\n"
    # Assessment text must not be interpreted as Actions workflow commands.
    token = uuid.uuid4().hex
    print(f"::stop-commands::{token}", flush=True)
    print(report, flush=True)
    print(f"::{token}::", flush=True)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        # GitHub limits each step summary to 1 MiB; the artifact and log retain every detail.
        summary = report.encode("utf-8")
        if len(summary) > 900_000:
            report = summary[:900_000].decode("utf-8", errors="ignore") + (
                "\n\nSummary shortened; full diagnostics are in this step's log and the "
                "monitor-diagnostics artifact for this attempt.\n"
            )
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(report)
    for problem in problems:
        print(f"::error::{problem}", flush=True)
    return int(bool(problems))


if __name__ == "__main__":
    raise SystemExit(main())
