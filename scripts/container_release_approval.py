"""Render candidate-bound, read-only context before protected image approval."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from container_release import (
    PREFIX,
    REPOSITORY,
    digest,
    profiles,
    read,
    timestamp,
    validate_identity,
    version,
)
from container_release_history import GitHub, History, decode
from container_release_publish import outputs
from prepare_container_release import run

WEB = f"https://github.com/{REPOSITORY}"
UNAVAILABLE = "Unavailable (not established by retained evidence)"
PROFILE_GUIDES = {
    "bicep": (
        "Bicep validation with the Bicep CLI and fallback configuration.",
        "images/bicep-sandbox/README.md",
    ),
    "bicep-avm": (
        "Offline Bicep validation with the Bicep CLI, fallback configuration and prepared Azure Verified Modules cache.",
        "images/bicep-sandbox/README.md#prepared-avm-profile",
    ),
    "sbx-bicep": (
        "Bicep validation with the Bicep base image and sbx runtime template.",
        "images/sbx-template/README.md",
    ),
    "graphviz": (
        "Offline graph rendering with Graphviz and fonts.",
        "images/graphviz-sandbox/README.md",
    ),
    "drawio-sandbox": (
        "Diagram authoring with Python and Graphviz.",
        "docs/sandbox/kinds/drawio.md",
    ),
    "drawio-export": (
        "Offline Draw.io export with Chromium, bundled Draw.io assets, fonts and the Python export helper.",
        "images/drawio-export/README.md",
    ),
    "terraform-random": (
        "Offline Terraform validation/planning with Python, the bounded runner and the random provider.",
        "images/terraform-sandbox/USAGE.md",
    ),
    "opentofu-random": (
        "Offline OpenTofu validation/planning with Python, the bounded runner and the random provider.",
        "images/terraform-sandbox/USAGE.md",
    ),
    "terraform-prepared": (
        "Terraform with Python, the bounded runner and the reviewed provider/module cache.",
        "images/terraform-sandbox/USAGE.md",
    ),
    "opentofu-prepared": (
        "OpenTofu with Python, the bounded runner and the reviewed provider/module cache.",
        "images/terraform-sandbox/USAGE.md",
    ),
    "hyperlight": (
        "Hyperlight AKS runtime with Python, workspace wheels, locked dependencies and the supervisor/probe.",
        "images/hyperlight-sandbox/README.md",
    ),
    "egress-proxy": (
        "Controlled sandbox egress with iron-proxy, the policy patch and CA certificates.",
        "docs/sandbox/backends/docker.md",
    ),
}


def literal(value: Any) -> str:
    """Keep evidence strings from introducing Markdown links, tables or HTML."""
    text = json.dumps(value, ensure_ascii=True) if not isinstance(value, str) else value
    return (
        "<code>"
        + html.escape(text).replace("|", "&#124;").replace("\n", "&#10;").replace("\r", "&#13;")
        + "</code>"
    )


def candidate_sha(candidate: dict[str, Any]) -> str:
    """Hash the canonical candidate bytes produced by the release writer."""
    raw = json.dumps(candidate, indent=2, sort_keys=True, allow_nan=False) + "\n"
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def publication_name(candidate: dict[str, Any]) -> str:
    """Name only the validated publication identity, including during recovery."""
    validate_identity(candidate)
    return f"Publish {candidate['profile']}:{candidate['version']} to public GHCR"


def artifact_link(run_id: str, name: str) -> str:
    """Resolve an exact retained artifact without treating expiry as success."""
    result = decode(
        GitHub().request(
            f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?name={name}&per_page=100"
        )
    )
    matches = result.get("artifacts", [])
    if result.get("total_count") != len(matches) or len(matches) > 1:
        raise ValueError("Ambiguous retained evidence artifact")
    if not matches or matches[0].get("expired") is not False:
        return UNAVAILABLE
    artifact = matches[0]
    if artifact.get("name") != name or type(artifact.get("id")) is not int:
        raise ValueError("Unexpected evidence artifact identity")
    return f"[Retained candidate/evidence artifact {artifact['id']}]({WEB}/actions/runs/{run_id}/artifacts/{artifact['id']})"


def bound_reports(directory: Path, candidate: dict[str, Any]) -> dict[str, Any]:
    """Display only original preparation reports whose bytes match the candidate's index."""
    index = directory / "preparation-index.json"
    if not index.is_file():
        return {}
    if digest(index) != candidate.get("preparationEvidenceSha256"):
        raise ValueError("Approval summary index differs from candidate")
    files = read(index)
    result = {}
    for name in ("runtime.json", "assessment.json", "grype.json", "sbom.syft.json", "build.json"):
        if name not in files:
            continue
        path = directory / f"approval-{name}"
        if not path.exists():
            path = directory / name
        if not path.is_file():
            continue
        if digest(path) != files[name]:
            raise ValueError(f"Approval summary evidence changed: {name}")
        result[name] = read(path)
    return result


def evidence_rows(reports: dict[str, Any], candidate: dict[str, Any]) -> list[tuple[str, Any]]:
    """Extract recorded outcomes without inferring success from missing fields."""
    runtime = reports.get("runtime.json", {})
    if runtime and (
        runtime.get("imageId") != candidate["imageId"]
        or runtime.get("profile") != candidate["profile"]
    ):
        raise ValueError("Runtime evidence identifies another candidate")
    assessment = reports.get("assessment.json", {})
    grype = reports.get("grype.json", {})
    inventory = reports.get("sbom.syft.json", {})
    database = assessment.get("database", {})
    rows: list[tuple[str, Any]] = [
        ("Runtime passed", runtime.get("passed", UNAVAILABLE)),
        ("Runtime check kind", runtime.get("kind", UNAVAILABLE)),
        ("Runtime checked at", runtime.get("checkedAt", UNAVAILABLE)),
        ("Cleanup outcome / remaining containers", runtime.get("containersRemaining", UNAVAILABLE)),
        (
            "Inventory packages (Syft)",
            len(inventory["artifacts"])
            if isinstance(inventory.get("artifacts"), list)
            else UNAVAILABLE,
        ),
        ("High/Critical gate, including unfixed findings", assessment.get("outcome", UNAVAILABLE)),
        ("Assessed at (UTC)", assessment.get("assessedAt", UNAVAILABLE)),
        ("Database source / identity", database.get("from", UNAVAILABLE)),
        ("Database schema", database.get("schemaVersion", UNAVAILABLE)),
        ("Database built at (UTC)", database.get("built", UNAVAILABLE)),
        ("Database valid", database.get("valid", UNAVAILABLE)),
    ]
    for check in runtime.get("bicepAvm", {}).get("checks", []):
        rows.append(("Bicep AVM check", check))
    if "bicepAvm" in runtime:
        rows.append(
            ("Prepared Bicep version", runtime["bicepAvm"].get("bicepVersion", UNAVAILABLE))
        )
        rows.append(("Prepared Bicep modules", runtime["bicepAvm"].get("modules", UNAVAILABLE)))
    if "result" in runtime:
        rows.append(("Runtime packaging evidence", runtime["result"]))
    if database.get("built") and assessment.get("assessedAt"):
        age = timestamp(assessment["assessedAt"]) - timestamp(database["built"])
        rows.append(("Database age at assessment (hours)", round(age.total_seconds() / 3600, 2)))
    else:
        rows.append(("Database age at assessment", UNAVAILABLE))
    matches = grype.get("matches")
    counts = (
        Counter(m.get("vulnerability", {}).get("severity", "Unclassified") for m in matches)
        if isinstance(matches, list)
        else None
    )
    for severity in ("Critical", "High", "Medium", "Low", "Negligible", "Unknown", "Unclassified"):
        rows.append(
            (
                f"Vulnerability matches: {severity}",
                counts[severity] if counts is not None else UNAVAILABLE,
            )
        )
    ignored = grype.get("ignoredMatches")
    rows.append(
        ("Ignored/suppressed matches", len(ignored) if isinstance(ignored, list) else UNAVAILABLE)
    )
    if ignored:
        rows.append(("Ignored/suppressed details", ignored))
    return rows


def render(
    candidate: dict[str, Any],
    directory: Path,
    operation: str,
    evidence: str,
    *,
    reason: str = "",
    stage: str = "",
) -> str:
    """Describe the retained candidate and the authority of one protected operation."""
    validate_identity(candidate)
    profile, release_version, source = (
        candidate[k] for k in ("profile", "version", "sourceCommit")
    )
    purpose, guide = PROFILE_GUIDES[profile]
    run_url = f"{WEB}/actions/runs/{candidate['attemptId']}"
    title = publication_name(candidate)
    if operation == "recover":
        title = f"Recover {stage} evidence for {profile}:{release_version}"
    elif operation == "abandon":
        title = f"Permanently abandon {profile}:{release_version}"
    reports = bound_reports(directory, candidate)
    rows = [
        ("Destination repository", f"{PREFIX}/{profile}"),
        ("Version", release_version),
        ("Required platform", "linux/amd64"),
        ("Recorded platform", reports.get("build.json", {}).get("platform", UNAVAILABLE)),
        ("Full manifest digest", candidate["registryDigest"]),
        ("Configuration digest", candidate["imageId"]),
        ("Candidate record SHA-256", candidate_sha(candidate)),
        (
            "Preparation evidence index SHA-256",
            candidate.get("preparationEvidenceSha256", UNAVAILABLE),
        ),
    ]
    rows += evidence_rows(reports, candidate)
    text = [
        f"# {title}",
        "",
        purpose,
        "",
        f"[Consumer guide]({WEB}/blob/{source}/{guide}) ; [Source revision and changes: {source}]({WEB}/commit/{source}) ; [Preparation results]({run_url})",
        "",
        evidence,
        "",
        "## Retained preparation evidence",
        "",
        "These are preparation results. They do not establish publication, post-publication consumer verification or live deployment support. Counts are scanner matches, not unique CVEs. Unavailable values are not passing results.",
        "",
        "| Field | Retained value |",
        "| --- | --- |",
    ]
    text.extend(f"| {label} | {literal(value)} |" for label, value in rows)
    text.extend(["", "## What approval authorizes", ""])
    if operation == "release":
        text.append(
            "Publish the retained bytes to the public GHCR destination above; sign provenance and SBOM evidence; verify signatures, anonymous pull and runtime behavior; commit completion, sign its record and deliver immutable release evidence. Preparation success alone does not establish any of these later steps."
        )
        text.append("")
        text.append(
            "Before a registry write, an assessment at least 24 hours old must be refreshed against these same bytes. A failed refresh blocks publication. Changed candidate bytes, source, profile/version or release policy require new approval. Both the displayed and refreshed reports are retained."
        )
    elif operation == "recover":
        text.append(
            f"Recover only the original {stage} storage boundary using the selected original evidence. This does not authorize rebuilding, a new registry image upload or replacement signatures. The writer revalidates the selection and original candidate-bound approval before writing."
        )
        text.append("")
        text.append(
            "For complete: retain qualified evidence and commit completion, then rerun the original workflow's signing/delivery jobs. For deliver: verify original attestations and publish immutable evidence, then rerun original reconciliation and consumer verification. The original same-bytes scan-refresh policy remains unchanged; recovery does not grant a new publication approval."
        )
    else:
        text.append(
            "Permanently retire this incomplete version and record the reason. It cannot be reused; a replacement requires a new version and publication approval. This does not publish, sign, rebuild or delete the retained image/evidence, and cannot abandon a completed release."
        )
        text.append("")
        text.append(f"Reason: {literal(reason)}")
    text.extend(
        [
            "",
            "## Open from GitHub Mobile",
            "",
            "If the approval screen omits this summary or the environment link, open this workflow run in a browser and select Summary. Read this evidence before returning to Approve/Reject. The retained artifact is a raw-evidence fallback; the summary does not require downloading it.",
            "",
        ]
    )
    return "\n".join(text)


def main() -> None:
    """Write a read-only summary before the protected approval job is queued."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("release", "recover", "abandon"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--profile", choices=profiles())
    parser.add_argument("--version")
    parser.add_argument("--reason", default="")
    args = parser.parse_args()
    stage = ""
    if args.operation == "abandon":
        version(args.version)
        head = History().head()
        record = head.catalogue["releases"].get(f"{args.profile}/{args.version}") if head else None
        if not record or record["state"] != "incomplete" or not args.reason.strip():
            raise ValueError("Abandonment requires an incomplete reservation and reason")
        candidate = record["candidate"]
        evidence = artifact_link(candidate["attemptId"], f"retained-image-{candidate['attemptId']}")
        if evidence != UNAVAILABLE:
            run(
                [
                    "gh",
                    "run",
                    "download",
                    candidate["attemptId"],
                    "--repo",
                    REPOSITORY,
                    "--name",
                    f"retained-image-{candidate['attemptId']}",
                    "--dir",
                    str(args.directory),
                ],
                capture_output=True,
            )
            if read(args.directory / "candidate.json") != candidate:
                raise ValueError("Abandonment evidence differs from the reserved candidate")
    elif args.operation == "recover":
        if args.selection is None:
            parser.error("recovery requires --selection")
        planned = read(args.selection)
        candidate, stage = planned["candidate"], planned["stage"]
        if read(args.directory / "candidate.json") != candidate:
            raise ValueError("Summary evidence differs from recovery selection")
        evidence = f"[Selected original evidence artifact {planned['artifactId']}]({WEB}/actions/runs/{planned['runId']}/artifacts/{planned['artifactId']})"
    else:
        if args.candidate is None:
            parser.error("release requires --candidate")
        candidate = read(args.candidate)
        evidence = artifact_link(candidate["attemptId"], f"retained-image-{candidate['attemptId']}")
    summary = render(
        candidate, args.directory, args.operation, evidence, reason=args.reason, stage=stage
    )
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
        stream.write(summary)
    outputs(
        {
            "approval_name": publication_name(candidate),
            "summary_url": f"{WEB}/actions/runs/{os.environ['GITHUB_RUN_ID']}/attempts/{os.environ['GITHUB_RUN_ATTEMPT']}#summary",
        }
    )


if __name__ == "__main__":
    main()
