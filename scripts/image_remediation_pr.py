"""Maintain one owned remediation branch with recoverable, fast-forward-only GitHub writes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from container_release import REPOSITORY, ROOT, read, write
from container_release_history import GitHub, decode, encode, positive_id
from image_remediation import proposal_files, qualify
from image_remediation_evidence import current

BRANCH = "bot/go-image-remediation"
MARKER = "maf-image-remediation-v1"
TITLE = "build: rebuild Go image dependencies with verified security fixes"


class BotGitHub(GitHub):
    """Use the installation token only for writes; evidence reads retain the workflow identity."""

    def request(self, endpoint: str, **kwargs: Any) -> bytes:
        if kwargs.get("method", "GET") == "GET":
            return super().request(endpoint, **kwargs)
        token = os.environ.get("IMAGE_REMEDIATION_TOKEN")
        if not token:
            raise ValueError("Remediation writes require the dedicated App token")
        previous = os.environ.get("GH_TOKEN")
        try:
            os.environ["GH_TOKEN"] = token
            return super().request(endpoint, **kwargs)
        finally:
            if previous is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = previous


def request(client: GitHub, path: str, **kwargs: Any) -> Any:
    """Keep every branch and pull-request operation inside the fixed repository."""
    return decode(client.request(f"repos/{REPOSITORY}/{path}", **kwargs))


def update_branch(client: GitHub, before: str | None, after: str) -> None:
    """Atomically require the observed head as well as a fast-forward update."""
    repository = decode(client.request(f"repos/{REPOSITORY}"))
    if repository.get("full_name") != REPOSITORY or not repository.get("node_id"):
        raise ValueError("Unexpected remediation repository identity")
    value = decode(
        client.request(
            "graphql",
            method="POST",
            payload=encode(
                {
                    "query": "mutation($input:UpdateRefsInput!){updateRefs(input:$input){clientMutationId}}",
                    "variables": {
                        "input": {
                            "repositoryId": repository["node_id"],
                            "clientMutationId": after,
                            "refUpdates": [
                                {
                                    "name": f"refs/heads/{BRANCH}",
                                    "beforeOid": before or "0" * 40,
                                    "afterOid": after,
                                    "force": False,
                                }
                            ],
                        }
                    },
                }
            ),
        )
    )
    if (
        value.get("errors")
        or value.get("data", {}).get("updateRefs", {}).get("clientMutationId") != after
    ):
        raise ValueError("Remediation branch compare-and-swap was not confirmed")


def marker(body: str) -> dict[str, Any]:
    """Require one complete ownership record, including interrupted-write state."""
    matches = re.findall(r"<!-- " + MARKER + r" (.+?) -->", body)
    if len(matches) != 1:
        raise ValueError("Pull request has no unique ownership marker")
    value = json.loads(matches[0])
    if value.get("schemaVersion") != 1:
        raise ValueError("Unsupported pull-request ownership marker")
    return value


def body(state: dict[str, Any], report: dict[str, Any], evidence_url: str) -> str:
    """Report residual findings without implying publication or closing release requests."""
    scans = [row for row in report["scans"] if row["variant"] == "candidate"]
    summary = "\n".join(
        f"- {row['profile']}: `{row['imageId']}`; assessment `{row['assessment']['outcome']}`."
        for row in scans
    )
    return (
        "## Summary\n\nRebuild Go module overlays and provider locks to remove the targeted "
        "image findings. All four affected profiles passed ordinary builds, runtime checks, "
        "inventory checks and rescans.\n\n## Related issue\n\nRefs #2514, #2516.\n\n"
        "## Changes\n\nOnly planned module overrides and regenerated provider locks change.\n\n"
        f"## Verification\n\nBase: `{report['source']}`. [Retained qualification]({evidence_url}).\n\n"
        f"{summary}\n\n## Notes\n\nUnrelated findings remain in the retained assessments. "
        "No image was published; deployed images and release-needed issues remain unchanged. "
        "Review the source diff and residual findings before merging.\n\n"
        f"<!-- {MARKER} {json.dumps(state, sort_keys=True, separators=(',', ':'))} -->\n"
    )


def owned(client: GitHub, head: str, state: dict[str, Any], bot: str) -> None:
    """Refuse changes outside the last recorded bot commit and its bounded file set."""
    if head != state.get("head"):
        raise ValueError("Remediation branch was modified outside automation")
    commit = request(client, f"commits/{head}")
    if commit.get("author", {}).get("login") != bot:
        raise ValueError("Remediation head is not authored by the configured App")
    compared = request(client, f"compare/{state['base']}...{head}")
    files = compared.get("files", [])
    if {item["filename"] for item in files} != state["files"].keys() or len(files) > 20:
        raise ValueError("Remediation branch contains unexpected files")
    for item in files:
        if item.get("status") != "modified" or item.get("previous_filename"):
            raise ValueError("Remediation branch changes file ownership")
        blob = request(client, f"git/blobs/{item['sha']}")
        if blob.get("encoding") != "base64" or blob.get("size", 0) > 4 * 1024**2:
            raise ValueError("Unexpected remediation blob")
        raw = base64.b64decode(blob["content"])
        if hashlib.sha256(raw).hexdigest() != state["files"][item["filename"]]:
            raise ValueError("Remediation branch content differs from its ownership marker")


def publish(
    client: GitHub,
    receipt: dict[str, Any],
    report: dict[str, Any],
    files: dict[str, bytes],
    bot: str,
    evidence_url: str,
) -> dict[str, Any]:
    """Reconcile uncertain writes on retry; never force a ref or reopen a human-closed PR."""
    if not re.fullmatch(r"[a-z0-9-]+\[bot\]", bot):
        raise ValueError("Expected a dedicated GitHub App bot identity")
    if not re.fullmatch(rf"https://github.com/{REPOSITORY}/actions/runs/[1-9][0-9]*", evidence_url):
        raise ValueError("Expected retained workflow evidence")
    if report["status"] != "ready" or report["source"] != receipt["source"]:
        raise ValueError("Proposal has not passed current-source image qualification")
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    if hashes != report["files"] or not hashes or len(hashes) > 20:
        raise ValueError("Qualified files differ from the proposed commit")
    current(client, receipt)
    pulls = client.pages(f"repos/{REPOSITORY}/pulls?state=all&head=sokolaidev:{BRANCH}&base=main")
    pulls.sort(key=lambda item: positive_id(item["number"]), reverse=True)
    pull = pulls[0] if pulls else None
    references = client.pages(f"repos/{REPOSITORY}/git/matching-refs/heads/{BRANCH}")
    exact = [item for item in references if item["ref"] == f"refs/heads/{BRANCH}"]
    if len(exact) > 1:
        raise ValueError("Ambiguous remediation branch")
    head = exact[0]["object"]["sha"] if exact else None
    previous = None
    if pull is not None:
        if (
            pull.get("user", {}).get("login") != bot
            or pull["head"]["repo"]["full_name"] != REPOSITORY
            or pull["head"]["ref"] != BRANCH
            or pull["base"]["ref"] != "main"
        ):
            raise ValueError("Remediation pull request is not owned by the configured App")
        if pull["state"] == "closed" and not pull.get("merged_at"):
            return {"status": "dismissed", "url": pull["html_url"]}
        state = marker(pull["body"] or "")
        previous = next(
            (
                item
                for item in [state.get("pending"), state.get("current")]
                if item is not None and item.get("head") == head
            ),
            None,
        )
        if head is not None:
            if previous is None:
                raise ValueError("Remediation branch was modified by another writer")
            owned(client, head, previous, bot)
        elif pull["state"] == "open":
            raise ValueError("Open remediation pull request lost its branch")
        if pull["state"] == "closed":
            pull = None
    base = receipt["source"]
    base_commit = request(client, f"git/commits/{base}")
    entries = []
    for name, raw in sorted(files.items()):
        blob = request(
            client,
            "git/blobs",
            method="POST",
            payload=encode(
                {
                    "encoding": "base64",
                    "content": base64.b64encode(raw).decode(),
                }
            ),
        )
        entries.append({"path": name, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = request(
        client,
        "git/trees",
        method="POST",
        payload=encode(
            {
                "base_tree": base_commit["tree"]["sha"],
                "tree": entries,
            }
        ),
    )
    if previous is not None and previous["base"] == base and previous["files"] == hashes and pull:
        state = {"schemaVersion": 1, "current": previous, "pending": None}
        request(
            client,
            f"pulls/{pull['number']}",
            method="PATCH",
            payload=encode({"body": body(state, report, evidence_url)}),
        )
        return {"status": "unchanged", "url": pull["html_url"]}
    user = decode(client.request(f"users/{bot}"))
    identity = {
        "name": bot,
        "email": f"{positive_id(user['id'])}+{bot}@users.noreply.github.com",
        "date": base_commit["committer"]["date"],
    }
    # A deterministic initial commit lets a retry recognize a branch created before a lost response.
    parents = [base] if previous is None else list(dict.fromkeys([head, base]))
    commit = request(
        client,
        "git/commits",
        method="POST",
        payload=encode(
            {
                "message": TITLE,
                "tree": tree["sha"],
                "parents": parents,
                "author": identity,
                "committer": identity,
            }
        ),
    )
    proposed = {"head": commit["sha"], "base": base, "files": hashes}
    if head is not None and previous is None and head != proposed["head"]:
        raise ValueError("Unclaimed remediation branch differs from the deterministic proposal")
    state = {"schemaVersion": 1, "current": previous, "pending": proposed}
    current(client, receipt)
    if pull:
        request(
            client,
            f"pulls/{pull['number']}",
            method="PATCH",
            payload=encode({"body": body(state, report, evidence_url)}),
        )
    if head != proposed["head"]:
        update_branch(client, head, proposed["head"])
    state = {"schemaVersion": 1, "current": proposed, "pending": None}
    current(client, receipt)
    if pull:
        result = request(
            client,
            f"pulls/{pull['number']}",
            method="PATCH",
            payload=encode({"body": body(state, report, evidence_url)}),
        )
    else:
        result = request(
            client,
            "pulls",
            method="POST",
            payload=encode(
                {
                    "title": TITLE,
                    "body": body(state, report, evidence_url),
                    "head": BRANCH,
                    "base": "main",
                    "draft": False,
                }
            ),
        )
    return {"status": "updated" if pull else "created", "url": result["html_url"]}


def main() -> None:
    """Publish only after revalidating the downloaded evidence with trusted-main tooling."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--bot", required=True)
    args = parser.parse_args()
    report = qualify(ROOT, args.work, args.results)
    if report != read(args.work / "qualified.json"):
        raise ValueError("Qualified report changed")
    result = publish(
        BotGitHub(),
        read(args.work / "receipt.json"),
        report,
        proposal_files(ROOT, args.work / "proposal"),
        args.bot,
        f"https://github.com/{REPOSITORY}/actions/runs/"
        f"{positive_id(int(os.environ['GITHUB_RUN_ID']))}",
    )
    write(args.work / "publication.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
