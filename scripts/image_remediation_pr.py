"""Maintain one owned remediation branch with recoverable, fast-forward-only GitHub writes."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from container_release import REPOSITORY, ROOT, read, write
from container_release_history import GitHub, HistoryAdvanced, decode, encode, positive_id
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from image_remediation import proposal_files, qualify
from image_remediation_evidence import EvidenceChanging, current, deferred

BRANCH = "bot/go-image-remediation"
MARKER = "maf-image-remediation-v1"
COMMIT_MARKER = "maf-image-remediation-commit-v1"
MARKER_PATTERN = r"<!-- " + MARKER + r" (.+?) -->"
TITLE = "build: rebuild Go image dependencies with verified security fixes"
PENDING_NOTICE = (
    "## Refresh incomplete\n\nThe qualification below is from the last completed refresh "
    "and does not qualify the current branch. Do not merge until the refresh completes.\n\n"
)


class BotGitHub(GitHub):
    """Use the installation token only for writes; evidence reads retain the workflow identity."""

    branch = BRANCH
    title = TITLE

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
                                    "name": f"refs/heads/{getattr(client, 'branch', BRANCH)}",
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


def branch_head(client: GitHub) -> str | None:
    """Read the exact owned ref without admitting similarly prefixed branch names."""
    branch = getattr(client, "branch", BRANCH)
    references = client.pages(f"repos/{REPOSITORY}/git/matching-refs/heads/{branch}")
    exact = [item for item in references if item["ref"] == f"refs/heads/{branch}"]
    if len(exact) > 1:
        raise ValueError("Ambiguous remediation branch")
    return exact[0]["object"]["sha"] if exact else None


def require_head(client: GitHub, expected: str | None) -> None:
    """Refuse PR writes when the live branch no longer names the admitted head."""
    if branch_head(client) != expected:
        raise ValueError("Remediation branch changed during refresh")


def marker(body: str) -> dict[str, Any]:
    """Require one complete ownership record, including interrupted-write state."""
    matches = re.findall(MARKER_PATTERN, body)
    if len(matches) != 1:
        raise ValueError("Pull request has no unique ownership marker")
    value = json.loads(matches[0])
    if value.get("schemaVersion") != 1:
        raise ValueError("Unsupported pull-request ownership marker")
    content = re.sub(MARKER_PATTERN, "", body)
    if value.get("bodySha256") != hashlib.sha256(content.encode()).hexdigest():
        raise ValueError("Pull-request body was modified outside automation")
    return value


def with_marker(text: str, state: dict[str, Any]) -> str:
    """Bind the surrounding prose to its ownership record without changing it."""
    if len(re.findall(MARKER_PATTERN, text)) != 1:
        raise ValueError("Pull request has no unique ownership marker")
    content = re.sub(MARKER_PATTERN, "", text)
    value = {**state, "bodySha256": hashlib.sha256(content.encode()).hexdigest()}
    return re.sub(
        MARKER_PATTERN,
        lambda _: f"<!-- {MARKER} {json.dumps(value, sort_keys=True, separators=(',', ':'))} -->",
        text,
    )


def latest_pull(client: GitHub) -> dict[str, Any] | None:
    """Include every base and refuse older open requests sharing the managed branch."""
    branch = getattr(client, "branch", BRANCH)
    pulls = client.pages(f"repos/{REPOSITORY}/pulls?state=all&head=sokolaidev:{branch}")
    latest = max(pulls, key=lambda item: positive_id(item["number"])) if pulls else None
    if any(item.get("state") == "open" and item is not latest for item in pulls):
        raise ValueError("Older open pull request competes for the remediation branch")
    return latest


def require_pull(client: GitHub, pull: dict[str, Any] | None) -> None:
    """Refuse changed ownership, disposition or text before branch and PR writes."""
    latest = latest_pull(client)
    if (latest or {}).get("number") != (pull or {}).get("number"):
        raise ValueError("Remediation pull-request ownership changed during refresh")
    if pull is None:
        return
    latest = request(client, f"pulls/{pull['number']}")
    if latest.get("body") != pull.get("body"):
        raise ValueError("Pull-request body changed during refresh")
    for value in ("state", "merged_at"):
        if latest.get(value) != pull.get(value):
            raise ValueError("Pull-request state changed during refresh")
    if (
        latest["user"]["login"] != pull["user"]["login"]
        or latest["head"]["repo"]["full_name"] != pull["head"]["repo"]["full_name"]
        or latest["head"]["ref"] != pull["head"]["ref"]
        or latest["base"]["ref"] != pull["base"]["ref"]
    ):
        raise ValueError("Pull-request ownership changed during refresh")


def update_body(
    client: GitHub, pull: dict[str, Any], text: str, head: str | None
) -> dict[str, Any]:
    """Refuse observed edits between ownership discovery and each body replacement."""
    require_pull(client, pull)
    require_head(client, head)
    result = request(
        client, f"pulls/{pull['number']}", method="PATCH", payload=encode({"body": text})
    )
    pull["body"] = text
    return result


def pending_body(text: str, state: dict[str, Any]) -> str:
    """Retain completed qualification while visibly withholding admission during a refresh."""
    return with_marker(PENDING_NOTICE + text.removeprefix(PENDING_NOTICE), state)


def body(state: dict[str, Any], report: dict[str, Any], evidence_url: str) -> str:
    """Report residual findings without implying publication or closing release requests."""
    scans = [row for row in report["scans"] if row["variant"] == "candidate"]
    summary = "\n".join(
        f"- {row['profile']}: `{row['imageId']}`; assessment `{row['assessment']['outcome']}`."
        for row in scans
    )
    platform = report.get("kind") == "platform"
    description = (
        "Update build-consumed Bicep or OS package pins to remove targeted image findings."
        if platform
        else "Rebuild Go module overlays and provider locks to remove the targeted image findings."
    )
    changes = (
        "Only planned package version pins change; compiler, runtime and base-image policies remain reviewed inputs."
        if platform
        else "Only planned module overrides and regenerated provider locks change."
    )
    issue = 2517 if platform else 2516
    text = (
        f"## Summary\n\n{description} All affected profiles passed ordinary builds, runtime checks, "
        f"inventory checks and rescans.\n\n## Related issue\n\nRefs #2514, #{issue}.\n\n"
        f"## Changes\n\n{changes}\n\n"
        f"## Verification\n\nQualified commit: `{state['current']['head']}`. "
        f"Base: `{report['source']}`. [Retained qualification]({evidence_url}).\n\n"
        f"{summary}\n\n## Notes\n\nUnrelated findings remain in the retained assessments. "
        "No image was published; deployed images and release-needed issues remain unchanged. "
        "Review the source diff and residual findings before merging.\n\n"
        f"<!-- {MARKER} {{}} -->\n"
    )
    return with_marker(text, state)


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


def signing_key() -> rsa.RSAPrivateKey:
    """Use the configured App key as the trust root for orphan ownership."""
    pem = os.environ.get("IMAGE_REMEDIATION_APP_PRIVATE_KEY")
    if not pem:
        raise ValueError("Remediation ownership requires the configured App private key")
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
        raise ValueError("Remediation ownership requires an RSA App key of at least 2048 bits")
    return key


def ownership_payload(client: GitHub, state: dict[str, Any]) -> bytes:
    """Bind signatures to this repository, publisher and ownership protocol."""
    return encode(
        {
            "purpose": COMMIT_MARKER,
            "repository": REPOSITORY,
            "title": getattr(client, "title", TITLE),
            "state": state,
        }
    )


def ownership_message(client: GitHub, envelope: dict[str, Any]) -> str:
    """Render the signed ownership envelope without altering its bytes."""
    return (
        f"{getattr(client, 'title', TITLE)}\n\n<!-- {COMMIT_MARKER} "
        f"{json.dumps(envelope, sort_keys=True, separators=(',', ':'))} -->"
    )


def commit_message(client: GitHub, state: dict[str, Any]) -> str:
    """Persist App-signed branch ownership before a pull request exists."""
    signature = signing_key().sign(
        ownership_payload(client, state),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return ownership_message(
        client, {"state": state, "signature": base64.b64encode(signature).decode()}
    )


def orphan(client: GitHub, head: str, bot: str) -> dict[str, Any]:
    """Admit only a marked bot commit with unchanged parents and bounded file contents."""
    commit = request(client, f"git/commits/{head}")
    matches = re.findall(r"<!-- " + COMMIT_MARKER + r" (.+?) -->", commit["message"])
    if len(matches) != 1:
        raise ValueError("Unclaimed remediation branch has no unique commit ownership marker")
    identity = request(client, f"commits/{head}")
    if any((identity.get(role) or {}).get("login") != bot for role in ("author", "committer")):
        raise ValueError("Unclaimed remediation branch is not authored and committed by the App")
    envelope = json.loads(matches[0])
    if not isinstance(envelope, dict) or set(envelope) != {"state", "signature"}:
        raise ValueError("Unclaimed remediation branch has no signed ownership record")
    state = envelope["state"]
    try:
        signing_key().public_key().verify(
            base64.b64decode(envelope["signature"], validate=True),
            ownership_payload(client, state),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    except (InvalidSignature, binascii.Error, TypeError, ValueError):
        raise ValueError("Unclaimed remediation branch lacks a valid App signature") from None
    if (
        not isinstance(state, dict)
        or set(state) != {"schemaVersion", "branch", "base", "files", "parents", "tree"}
        or state["schemaVersion"] != 1
        or state["branch"] != getattr(client, "branch", BRANCH)
        or not isinstance(state["base"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", state["base"])
        or state["tree"] != commit["tree"]["sha"]
        or not isinstance(state["files"], dict)
        or not 1 <= len(state["files"]) <= 20
        or not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
            for value in state["files"].values()
        )
        or state["parents"] != [item["sha"] for item in commit["parents"]]
        or len(state["parents"]) not in {1, 2}
        or state["parents"][-1] != state["base"]
        or commit["message"] != ownership_message(client, envelope)
    ):
        raise ValueError("Unclaimed remediation branch has invalid commit ownership")
    previous = {"head": head, "base": state["base"], "files": state["files"]}
    owned(client, head, previous, bot)
    return previous


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
    if not re.fullmatch(
        rf"https://github.com/{REPOSITORY}/actions/runs/[1-9][0-9]*/artifacts/[1-9][0-9]*",
        evidence_url,
    ):
        raise ValueError("Expected retained workflow evidence")
    if report["status"] != "ready" or report["source"] != receipt["source"]:
        raise ValueError("Proposal has not passed current-source image qualification")
    hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    if hashes != report["files"] or not hashes or len(hashes) > 20:
        raise ValueError("Qualified files differ from the proposed commit")
    current(client, receipt)
    pull = latest_pull(client)
    ownership_pull = pull
    ownership_state = None
    head = branch_head(client)
    previous = None
    if pull is not None:
        if (
            pull.get("user", {}).get("login") != bot
            or pull["head"]["repo"]["full_name"] != REPOSITORY
            or pull["head"]["ref"] != getattr(client, "branch", BRANCH)
            or pull["base"]["ref"] != "main"
        ):
            raise ValueError("Remediation pull request is not owned by the configured App")
        if pull["state"] == "closed" and not pull.get("merged_at"):
            return {"status": "dismissed", "url": pull["html_url"]}
        state = marker(pull["body"] or "")
        ownership_state = state
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
    elif head is not None:
        previous = orphan(client, head, bot)
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
        current(client, receipt)
        state = {"schemaVersion": 1, "current": previous, "pending": None}
        update_body(client, pull, body(state, report, evidence_url), head)
        return {"status": "unchanged", "url": pull["html_url"]}
    user = decode(client.request(f"users/{bot}"))
    identity = {
        "name": bot,
        "email": f"{positive_id(user['id'])}+{bot}@users.noreply.github.com",
        "date": base_commit["committer"]["date"],
    }
    parents = [base] if previous is None else list(dict.fromkeys([head, base]))
    commit_state = {
        "schemaVersion": 1,
        "branch": getattr(client, "branch", BRANCH),
        "base": base,
        "files": hashes,
        "parents": parents,
        "tree": tree["sha"],
    }
    commit = request(
        client,
        "git/commits",
        method="POST",
        payload=encode(
            {
                "message": commit_message(client, commit_state),
                "tree": tree["sha"],
                "parents": parents,
                "author": identity,
                "committer": identity,
            }
        ),
    )
    proposed = {"head": commit["sha"], "base": base, "files": hashes}
    if previous is not None and previous["base"] == base and previous["files"] == hashes:
        proposed = previous
    state = {"schemaVersion": 1, "current": previous, "pending": proposed}
    current(client, receipt)
    if pull:
        update_body(client, pull, pending_body(pull["body"], state), head)
    elif ownership_pull and ownership_state:
        # Keep the merged PR's qualification intact while recording the next branch write.
        ownership_state["pending"] = proposed
        update_body(
            client, ownership_pull, with_marker(ownership_pull["body"], ownership_state), head
        )
    if head != proposed["head"]:
        require_pull(client, ownership_pull)
        update_branch(client, head, proposed["head"])
    state = {"schemaVersion": 1, "current": proposed, "pending": None}
    current(client, receipt)
    if pull:
        result = update_body(client, pull, body(state, report, evidence_url), proposed["head"])
    else:
        require_pull(client, ownership_pull)
        require_head(client, proposed["head"])
        result = request(
            client,
            "pulls",
            method="POST",
            payload=encode(
                {
                    "title": getattr(client, "title", TITLE),
                    "body": body(state, report, evidence_url),
                    "head": getattr(client, "branch", BRANCH),
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
    parser.add_argument("--artifact-id", type=int, required=True)
    args = parser.parse_args()
    report = qualify(ROOT, args.work, args.results)
    if report != read(args.work / "qualified.json"):
        raise ValueError("Qualified report changed")
    try:
        result = publish(
            BotGitHub(),
            read(args.work / "receipt.json"),
            report,
            proposal_files(ROOT, args.work / "proposal"),
            args.bot,
            f"https://github.com/{REPOSITORY}/actions/runs/"
            f"{positive_id(int(os.environ['GITHUB_RUN_ID']))}/artifacts/{positive_id(args.artifact_id)}",
        )
    except (EvidenceChanging, HistoryAdvanced) as error:
        result = deferred(args.work, error, "publish")
    write(args.work / "publication.json", result)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
