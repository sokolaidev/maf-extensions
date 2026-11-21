"""Notify maintainers when retained image findings have no delivered source remedy."""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import re
import subprocess
import zipfile
from collections import Counter
from datetime import datetime
from typing import Any

from report_workflow_failure import gh, notification_marker, unchanged

WORKFLOWS = {
    "platform": "platform-image-remediation.yml",
    "go": "image-remediation.yml",
    "chrome": "chrome-image-update.yml",
    "scan": "image-security.yml",
}
DOCUMENTS = {
    "plan.json",
    "receipt.json",
    "qualified.json",
    "publication.json",
    "deferral.json",
    "competing-pulls.json",
    "delivery-requirement.json",
    "build.json",
    "assessment.json",
    "grype.json",
}
REBUILD = {"rebuild-required", "source-pin-sufficient", "override-already-sufficient"}


def artifact_names(kind: str, run_id: str, attempt: str) -> list[str]:
    """Order acquisition, qualification and delivery snapshots by authority."""
    if kind == "scan":
        return []
    if kind == "chrome":
        return [f"chrome-update-{run_id}-{attempt}", f"chrome-update-delivery-{run_id}-{attempt}"]
    prefix = "platform-remediation" if kind == "platform" else "remediation"
    return [
        f"{prefix}-plan",
        *(["remediation-inputs"] if kind == "go" else []),
        f"{prefix}-qualification-{run_id}-{attempt}",
        f"{prefix}-delivery-{run_id}-{attempt}",
    ]


def documents(raw: bytes) -> dict[str, Any]:
    """Read only notification documents; never extract or execute archive content."""
    result = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            name = entry.filename.removeprefix("remediation/")
            if name not in DOCUMENTS:
                continue
            if name in result or entry.file_size > 32 * 1024 * 1024:
                raise ValueError("Duplicate or oversized notification document")
            result[name] = json.loads(archive.read(entry))
    return result


def collect(kind: str, repo: str, run_id: str, attempt: str) -> tuple[dict, dict]:
    """Use artifacts from this trusted default-branch attempt only."""
    run = json.loads(gh(f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}"))
    if (
        run["head_branch"] != "main"
        or run["head_repository"]["full_name"] != repo
        or run["event"] not in {"schedule", "workflow_dispatch", "workflow_run"}
        or run["path"] != f".github/workflows/{WORKFLOWS[kind]}"
    ):
        raise ValueError("Notification source must be the expected trusted main workflow")
    started = datetime.fromisoformat(run["run_started_at"])
    pages = json.loads(
        gh(f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100", "--paginate", "--slurp")
    )
    available = {}
    for page in pages:
        for item in page["artifacts"]:
            if not item["expired"] and datetime.fromisoformat(item["created_at"]) >= started:
                name = item["name"]
                if name in available:
                    raise ValueError("Ambiguous artifact for this attempt")
                available[name] = item
    names = artifact_names(kind, run_id, attempt)
    if kind == "scan":
        names = sorted(
            name for name in available if re.fullmatch(r"image-security-[a-z0-9-]+", name)
        )
    result: dict[str, Any] = {}
    for name in names:
        if name not in available:
            continue
        artifact = available[name]
        if artifact["size_in_bytes"] > 150 * 1024 * 1024:
            raise ValueError("Notification artifact exceeds size limit")
        raw = subprocess.check_output(
            ["gh", "api", f"repos/{repo}/actions/artifacts/{int(artifact['id'])}/zip"]
        )
        values = documents(raw)
        if kind == "scan":
            result[name.removeprefix("image-security-")] = values
        else:
            result.update(values)
    return run, result


def scan_rows(values: dict, run_id: str, attempt: str) -> list[dict]:
    """Preserve reviewed non-applicability when explaining a failing build scan."""
    assessment = values.get("assessment.json", {})
    if assessment.get("outcome") != "vulnerable":
        return []
    build = values["build.json"]
    if str(build["run_id"]) != run_id or str(build["run_attempt"]) != attempt:
        raise ValueError("Scan belongs to another attempt")
    excluded = {(row["id"], row["artifactId"]) for row in assessment.get("notAffected", [])}
    rows = []
    for match in values["grype.json"]["matches"]:
        vulnerability, component = match["vulnerability"], match["artifact"]
        if (
            vulnerability["severity"] not in {"High", "Critical"}
            or (vulnerability["id"], component["id"]) in excluded
        ):
            continue
        rows.append(
            {
                "profile": build["profile"],
                "id": vulnerability["id"],
                "component": component["name"],
                "installed": component["version"],
                "fix": vulnerability.get("fix") or {},
                "severity": vulnerability["severity"],
                "reason": "Build scan has no PR delivery stage. Check remediation coverage and deliver a fix or document the blocker.",
            }
        )
    if Counter((r["id"], r["severity"]) for r in rows) != Counter(
        (r["id"], r["severity"]) for r in assessment["findings"]
    ):
        raise ValueError("Findings differ from the retained applicability assessment")
    return rows


def pending(values: dict, kind: str) -> list[dict]:
    """A delivered partial proposal covers only its update decisions."""
    publication = values.get("publication.json", {})
    delivered = publication.get("status") in {"created", "updated", "unchanged"} and bool(
        publication.get("url")
    )
    qualified = values.get("qualified.json", {}).get("status", "not-qualified")
    rows = []
    for finding in values.get("plan.json", {}).get("findings", []):
        decisions = finding.get("decisions", [{"status": finding.get("status", "unknown")}])
        statuses = {d["status"] for d in decisions} or {"unknown"}
        if delivered and qualified == "ready":
            statuses.discard("update")
        if not statuses:
            continue
        reasons = []
        for status in sorted(statuses):
            if status in REBUILD:
                reason = "Publish a replacement image after release qualification; the recipe may already contain the targeted fix."
                if qualified == "release-needed":
                    reason = "Rebuild qualification removed the targeted finding without a source edit. Publish a replacement image; other findings may remain."
            elif status == "update":
                reason = f"Source update planned but no qualified PR was delivered (qualification: {qualified})."
            elif status == "no-compatible-fix":
                reason = "No compatible fix identified in retained evidence. Check vendor advisories and package availability; this does not prove no upstream fix exists."
            else:
                reason = "Automation cannot deliver this finding's fix; investigate the component or extend remediation coverage."
            reasons.append(f"{status}: {reason}")
        rows.append(
            {
                "profile": finding["profile"],
                "id": finding["id"],
                "component": finding.get("component", finding.get("module", "unknown")),
                "installed": finding["installed"],
                "fix": finding.get("fix", {}),
                "reason": " ".join(reasons),
            }
        )
    if kind == "chrome" and not delivered:
        requirement = values.get("delivery-requirement.json", {})
        if requirement.get("kind") == "security":
            receipt = values.get("receipt.json", {})
            rows.append(
                {
                    "profile": "document-export",
                    "id": "Chrome security update",
                    "component": "chrome",
                    "installed": receipt.get("before", "see evidence"),
                    "fix": {
                        "versions": [receipt.get("package", {}).get("version", "see evidence")]
                    },
                    "reason": "A security update was authorized but PR delivery did not complete. Inspect delivery settings, credentials and logs.",
                }
            )
    return rows


def cell(value: Any) -> str:
    """Keep evidence text inert in issue tables and avoid accidental mentions."""
    text = " ".join(str(value).split())
    if len(text) > 240:
        text = text[:237] + "..."
    escaped = html.escape(text).replace("|", "&#124;").replace("@", "&#64;").replace("`", "&#96;")
    return escaped if len(escaped) <= 240 else escaped[:237] + "..."


def notify(kind: str, run: dict, values: dict, repo: str, run_id: str, attempt: str) -> None:
    """Deduplicate notifications without treating source delivery as published recovery."""
    rows = (
        [row for data in values.values() for row in scan_rows(data, run_id, attempt)]
        if kind == "scan"
        else pending(values, kind)
    )
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        row["fix"] = {
            "state": row["fix"].get("state", "unknown"),
            "versions": sorted(set(row["fix"].get("versions") or [])),
        }
        grouped.setdefault(row["profile"], []).append(row)
    if not grouped:
        print("No retained findings require a new maintainer notification.")
        return
    endpoint = f"repos/{repo}/issues"
    pages = json.loads(gh(f"{endpoint}?state=open&per_page=100", "--paginate", "--slurp"))
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}/attempts/{attempt}"
    for profile, findings in sorted(grouped.items()):
        if not re.fullmatch(r"[a-z0-9-]+", profile):
            raise ValueError("Invalid image profile")
        marker = f"<!-- image-remediation-attention:{kind}:{profile} -->"
        tracker = next(
            (
                issue
                for page in pages
                for issue in page
                if "pull_request" not in issue
                and issue.get("user", {}).get("login") == "github-actions[bot]"
                and marker in (issue.get("body") or "")
            ),
            None,
        )
        stable = sorted({json.dumps(row, sort_keys=True) for row in findings})
        state = notification_marker(json.dumps(stable))
        if tracker is not None and unchanged(tracker, endpoint, state):
            continue
        lines = [
            "| Finding | Component | Installed | Reported fixes | Why maintainer action is needed |",
            "|---|---|---|---|---|",
        ]
        for raw in stable[:40]:
            row = json.loads(raw)
            fixes = (
                ", ".join(row["fix"].get("versions") or []) or "Not identified in retained evidence"
            )
            lines.append(
                "| "
                + " | ".join(
                    cell(v)
                    for v in (row["id"], row["component"], row["installed"], fixes, row["reason"])
                )
                + " |"
            )
        details = "\n".join(lines)
        if len(stable) > 40:
            details += f"\n\nShowing 40 of {len(stable)} finding/component records; the linked artifacts retain all findings."
        context = ""
        for name in ("deferral.json", "competing-pulls.json", "publication.json"):
            if name in values:
                context += f"\n\n{cell(name)}: {cell(json.dumps(values[name], sort_keys=True))}"
        message = (
            f"{marker}\n\n**Describe the bug**\nImage `{profile}` needs maintainer action after {cell(run['name'])}. "
            f"[Run, logs and retained evidence]({run_url}).\n\n{details}{context}\n\n"
            "**To Reproduce**\nInspect the linked attempt's plan, qualification and delivery artifacts, or its build inventory and applicability assessment.\n\n"
            "**Expected behavior**\nDeliver a qualified source fix, publish a qualified replacement when only a rebuild is needed, or record the remaining blocker and follow-up.\n\n"
            f"**Environment**\n- Image profile: {profile}\n- Workflow commit: {cell(run['head_sha'])}\n"
            "- Image/package versions: shown above and in the retained evidence.\n- Python version: see the source workflow setup logs and inventory.\n\n"
            "**Additional context**\nThis notification does not publish images, waive findings, or establish recovery. "
            "A PR may cover only some findings. Maintainers close this issue after verifying the appropriate recovery evidence.\n\n"
            f"{state}"
        )
        if tracker is None:
            gh(
                endpoint,
                body={
                    "title": f"Image remediation needs maintainer action: {profile} ({kind})",
                    "body": message,
                },
            )
        else:
            gh(f"{endpoint}/{tracker['number']}/comments", body={"body": message})


def main() -> None:
    """Report the current attempt using its retained notification documents."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=WORKFLOWS, required=True)
    args = parser.parse_args()
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id, attempt = os.environ["GITHUB_RUN_ID"], os.environ["GITHUB_RUN_ATTEMPT"]
    run, values = collect(args.kind, repo, run_id, attempt)
    notify(args.kind, run, values, repo, run_id, attempt)


if __name__ == "__main__":
    main()
