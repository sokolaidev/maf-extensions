"""Generate the prepared AVM dependency manifest from a policy file; run via the CLI.

The policy names what to bake — provider addresses and registry-module sources with version
constraints — and this script resolves every pin: exact versions, artifact URLs, digests
cross-checked against the authoritative release SHA256SUMS, tag-to-commit revisions, module
call graphs and GitHub repository ids. The manifest stays committed; its diff is the review
surface, so generation is deterministic for identical policy and registry state. Nothing is
written before a full dry-run of the real preparer has proven every resolved pin.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import posixpath
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import terraform_dependencies as prep  # noqa: E402

REGISTRY = "https://registry.terraform.io"
GITHUB_API = "https://api.github.com"
MAX_BODY = 64 * 1024 * 1024
GIT_REF = re.compile(r"git::https://github\.com/([^/]+)/([^/?]+)\?ref=([0-9a-f]{40})\Z")
RELEASE = re.compile(r"\d+\.\d+\.\d+\Z")
SUMS_LINE = re.compile(r"([0-9a-f]{64})\s+\*?(.+)\Z")
GITHUB_RELEASE = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)\Z"
)


def http_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    """GET one bounded body, retrying transient failures the way the research probes do."""
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers=headers or {}), timeout=120) as response:
                data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError(f"response over {MAX_BODY} bytes: {url}")
            return data
        except HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise
        except OSError:
            pass
        if attempt < 4:
            time.sleep(2**attempt)
    raise ValueError(f"unavailable: {url}")


def http_json(url: str, headers: dict[str, str] | None = None) -> Any:
    """GET one JSON document."""
    return json.loads(http_bytes(url, headers))


def github_headers() -> dict[str, str]:
    """Accept GitHub's JSON flavor and authenticate when GITHUB_TOKEN is set."""
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def exact_keys(value: object, keys: set[str], what: str) -> dict[str, Any]:
    """Reject unknown or missing policy fields rather than guessing."""
    if not isinstance(value, dict) or value.keys() != keys:
        raise ValueError(f"{what} must carry exactly {sorted(keys)}")
    return value


def satisfies(version: str, constraint: str, context: str) -> bool:
    """Check one version against one Terraform constraint with the preparer's grammar."""
    try:
        return prep.satisfies(version, constraint, "policy-constraint")
    except prep.Refused:
        raise ValueError(f"{context}: invalid constraint {constraint!r}") from None


def newest_release(versions: list[str], constraint: str, context: str) -> str:
    """Pick the newest X.Y.Z release the constraint admits; prereleases are never pins."""
    releases = [item for item in versions if RELEASE.fullmatch(item)]
    for version in sorted(
        releases, key=lambda item: tuple(map(int, item.split("."))), reverse=True
    ):
        if satisfies(version, constraint, context):
            return version
    raise ValueError(f"{context}: no release satisfies {constraint!r} among {sorted(releases)}")


def full_address(value: str, what: str, *, modules: bool) -> str:
    """Require one full registry.terraform.io address; modules carry three name segments."""
    tail = prep._REGISTRY_PACKAGE if modules else prep._NAME + "/" + prep._NAME
    if not isinstance(value, str) or not re.fullmatch(
        re.escape(prep.REGISTRY_HOST) + "/" + tail, value
    ):
        raise ValueError(f"{what}: not a registry.terraform.io address: {value!r}")
    return value


def provider_versions(address: str) -> list[str]:
    """List every X.Y.Z release the registry publishes for linux_amd64."""
    _, namespace, kind = address.split("/")
    body = http_json(f"{REGISTRY}/v1/providers/{namespace}/{kind}/versions")
    return [
        item["version"]
        for item in body["versions"]
        if RELEASE.fullmatch(item["version"])
        and {"os": "linux", "arch": "amd64"} in item.get("platforms", [])
    ]


def module_versions(source: str) -> list[str]:
    """List every X.Y.Z release the registry publishes for one module."""
    namespace, name, system = source.split("/")[-3:]
    body = http_json(f"{REGISTRY}/v1/modules/{namespace}/{name}/{system}/versions")
    return [
        item["version"]
        for item in body["modules"][0]["versions"]
        if RELEASE.fullmatch(item["version"])
    ]


def checksum_for(filename: str, sums_url: str) -> str:
    """Find the file's digest in the authoritative SHA256SUMS document."""
    for line in http_bytes(sums_url).decode("utf-8").splitlines():
        if match := SUMS_LINE.fullmatch(line.strip()):
            if match.group(2) == filename:
                return match.group(1)
    raise ValueError(f"{filename} is missing from {sums_url}")


def repository_id(owner: str, repo: str) -> str:
    """Return the numeric repository id the preparer's redirect check requires."""
    return str(http_json(f"{GITHUB_API}/repos/{owner}/{repo}", github_headers())["id"])


def resolve_provider(address: str, constraint: str) -> dict[str, Any]:
    """Pin one provider: newest release, digest cross-check, repository id when on GitHub."""
    _, namespace, kind = address.split("/")
    version = newest_release(provider_versions(address), constraint, address)
    meta = http_json(f"{REGISTRY}/v1/providers/{namespace}/{kind}/{version}/download/linux/amd64")
    if meta["shasum"] != checksum_for(meta["filename"], meta["shasums_url"]):
        raise ValueError(
            f"{address} {version}: registry digest disagrees with the release SHA256SUMS"
        )
    url = meta["download_url"]
    match = GITHUB_RELEASE.fullmatch(url)
    if match is None:
        if not url.startswith("https://releases.hashicorp.com/"):
            raise ValueError(f"{address}: unsupported download host: {url}")
        source_label, tag = "HashiCorp", version
    else:
        source_label, tag = match.group(1), match.group(3)
    provenance = (
        f"{source_label} {kind} {tag} release SHA256SUMS matching registry download metadata"
    )
    artifact: dict[str, Any] = {"url": url, "sha256": meta["shasum"], "provenance": provenance}
    if match is not None:
        artifact["github_repository_id"] = repository_id(match.group(1), match.group(2))
    return {"source": address, "version": version, "platform": "linux_amd64", "artifact": artifact}


def module_call_graph(
    data: bytes, prefix: str, context: str
) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, str, str | None]]]:
    """Parse the package from its root: local edges in the graph, registry calls listed.

    Returns the directory graph with local edges resolved and every registry call as
    (directory, label, full address, version constraint).
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {
            item.filename[len(prefix) :]: item
            for item in archive.infolist()
            if item.filename.startswith(prefix) and not item.is_dir()
        }
        graph: dict[str, dict[str, Any]] = {}
        registry_calls: list[tuple[str, str, str, str | None]] = []
        pending = ["."]
        while pending:
            directory = pending.pop()
            if directory in graph:
                continue
            graph[directory] = {}
            names = sorted(name for name in files if (posixpath.dirname(name) or ".") == directory)
            if not any(name.endswith((".tf", ".tf.json")) for name in names):
                raise ValueError(f"{context}: directory {directory} has no configuration files")
            for name in names:
                stem = posixpath.basename(name).removesuffix(".json")
                if stem.endswith((".tofu", ".tofu.json")):
                    raise ValueError(f"{context}: registry packages must be Terraform: {name}")
                if stem == "override" or stem.endswith("_override"):
                    raise ValueError(f"{context}: override files are refused: {name}")
                if not name.endswith((".tf", ".tf.json")):
                    continue
                text = archive.read(files[name]).decode("utf-8")
                try:
                    parsed = json.loads(text) if name.endswith(".json") else prep.hcl2.loads(text)
                except Exception:
                    parsed = prep.hcl2.loads(text.replace("\r\n", "\n"))
                json_syntax = name.endswith(".json")
                for label, block in prep._blocks(parsed, "module", json_syntax):
                    source = prep._literal(block.get("source"), json_syntax)
                    if source is None:
                        raise ValueError(
                            f"{context}: module {label!r} in {directory} has a dynamic source"
                        )
                    if source.startswith(("./", "../")):
                        target = posixpath.normpath(posixpath.join(directory, source))
                        if target.startswith("../") or target == "..":
                            raise ValueError(f"{context}: module {label!r} escapes the package")
                        graph[directory][label] = {"local": target}
                        pending.append(target)
                        continue
                    match = re.fullmatch(rf"(?:([^/]+)/)?({prep._REGISTRY_PACKAGE})", source)
                    if (
                        match is None
                        or (match.group(1) or prep.REGISTRY_HOST).lower() != prep.REGISTRY_HOST
                    ):
                        raise ValueError(
                            f"{context}: module {label!r} calls an unsupported source {source!r}"
                        )
                    constraint = prep._literal(block.get("version"), json_syntax)
                    registry_calls.append(
                        (directory, label, f"{prep.REGISTRY_HOST}/{match.group(2)}", constraint)
                    )
    for directory in sorted(graph):
        graph[directory] = {label: graph[directory][label] for label in sorted(graph[directory])}
    return graph, registry_calls


def terraform_get(url: str) -> str:
    """Return the X-Terraform-Get location of one registry module download."""
    with urlopen(Request(url), timeout=120) as response:
        return response.headers.get("X-Terraform-Get", "")


def resolve_module(source: str, version: str) -> dict[str, Any]:
    """Resolve one registry module version to its commit archive and read its call graph."""
    namespace, name, system = source.split("/")[-3:]
    location = terraform_get(
        f"{REGISTRY}/v1/modules/{namespace}/{name}/{system}/{version}/download"
    )
    match = GIT_REF.fullmatch(location)
    if match is None:
        raise ValueError(
            f"{source} {version}: registry download is not a pinned commit: {location!r}"
        )
    owner, repo, revision = match.groups()
    data = http_bytes(f"https://codeload.github.com/{owner}/{repo}/zip/{revision}")
    context = f"{source} {version}"
    graph, registry_calls = module_call_graph(data, f"{repo}-{revision}/", context)
    return {
        "source": source,
        "version": version,
        "owner": owner,
        "repo": repo,
        "revision": revision,
        "sha256": hashlib.sha256(data).hexdigest(),
        "graph": graph,
        "registry_calls": registry_calls,
    }


def resolve_modules(policies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resolve the policy's registry modules and their pinned registry dependencies."""
    policy = {}
    for entry in policies:
        exact_keys(entry, {"source", "constraint"}, "registry module")
        address = full_address(entry["source"], "registry module", modules=True)
        if address.casefold() in policy:
            raise ValueError(f"duplicate registry module in policy: {address}")
        policy[address.casefold()] = entry
    records: dict[str, dict[str, Any]] = {}
    pending = list(policy)
    while pending:
        key = pending.pop(0)
        if key in records:
            continue
        entry = policy[key]
        record = resolve_module(
            entry["source"],
            newest_release(module_versions(entry["source"]), entry["constraint"], entry["source"]),
        )
        for _, _, address, _ in record["registry_calls"]:
            if address.casefold() not in policy:
                raise ValueError(
                    f"{entry['source']}: calls {address}, which the policy does not list to bake"
                )
            pending.append(address.casefold())
        records[key] = record
    modules = []
    for key in sorted(records):
        record = records[key]
        name = record["source"].split("/")[-2]
        for directory, label, address, constraint in record["registry_calls"]:
            target = records[address.casefold()]
            if constraint is None:
                raise ValueError(f"{record['source']}: module {label!r} does not pin a version")
            if not satisfies(target["version"], constraint, f"{record['source']} -> {address}"):
                raise ValueError(
                    f"{record['source']}: call to {address} requires {constraint}, "
                    f"but the policy resolves {target['version']}"
                )
            record["graph"][directory][label] = {"registry": target["source"].split("/")[-2]}
        modules.append(
            {
                "name": name,
                "source": record["source"],
                "version": record["version"],
                "revision": record["revision"],
                "graph": record["graph"],
                "artifact": {
                    "url": f"https://codeload.github.com/{record['owner']}/{record['repo']}/zip/{record['revision']}",
                    "sha256": record["sha256"],
                    "provenance": (
                        f"Registry {record['version']} download and tag v{record['version']} "
                        "resolve to this commit"
                    ),
                },
            }
        )
    names = [item["name"] for item in modules]
    if len(names) != len(set(names)):
        raise ValueError("policy modules resolve to colliding names")
    return modules


def dry_run(document: dict[str, Any]) -> None:
    """Prove every pin against real bytes before the manifest may change."""
    with tempfile.TemporaryDirectory() as temporary:
        prep.prepare(copy.deepcopy(document), Path(temporary) / "prepared")


def graph_text(graph: dict[str, dict[str, Any]], pad: int) -> str:
    """Compact graph text: fully inline when trivial, else one directory per block."""
    indent, inner, edge_indent = " " * pad, " " * (pad + 2), " " * (pad + 4)
    if all(not edges for edges in graph.values()) and len(json.dumps(graph)) <= 60:
        return json.dumps(graph)
    rows = []
    for name in sorted(graph):
        edges = dict(sorted(graph[name].items()))
        if not edges:
            rows.append(f"{json.dumps(name)}: {{}}")
            continue
        edge_rows = [f"{json.dumps(label)}: {json.dumps(edges[label])}" for label in sorted(edges)]
        rows.append(
            f"{json.dumps(name)}: {{\n"
            + ",\n".join(edge_indent + row for row in edge_rows)
            + f"\n{inner}}}"
        )
    return "{\n" + ",\n".join(inner + row for row in rows) + f"\n{indent}}}"


def render(value: Any, pad: int = 0, key: str | None = None) -> str:
    """Serialize deterministically; graphs stay compact so pin diffs stay readable."""
    indent = " " * pad
    inner = " " * (pad + 2)
    if key == "graph" and isinstance(value, dict):
        return graph_text(value, pad)
    if isinstance(value, dict):
        if not value:
            return "{}"
        rows = [
            inner + f"{json.dumps(name)}: {render(item, pad + 2, key=name)}"
            for name, item in value.items()
        ]
        return "{\n" + ",\n".join(rows) + f"\n{indent}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        rows = [inner + render(item, pad + 2) for item in value]
        return "[\n" + ",\n".join(rows) + f"\n{indent}]"
    return json.dumps(value)


def pin_changes(previous: dict[str, Any] | None, document: dict[str, Any]) -> list[str]:
    """Summarize what a refresh moved, so the operator reads the diff without opening it."""
    if previous is None:
        return ["manifest created"]
    lines: list[str] = []
    before = {item["source"]: item for item in previous.get("providers", [])}
    for item in document["providers"]:
        old = before.get(item["source"], {}).get("version")
        if old != item["version"]:
            lines.append(f"provider {item['source']}: {old} -> {item['version']}")
    old_modules = {item["source"]: item for item in previous.get("registry_modules", [])}
    for item in document["registry_modules"]:
        old = old_modules.get(item["source"])
        if old is None:
            lines.append(f"module {item['source']}: added at {item['version']}")
        elif old["version"] != item["version"] or old["revision"] != item["revision"]:
            lines.append(f"module {item['source']}: {old['version']} -> {item['version']}")
    for source in sorted(
        set(old_modules) - {item["source"] for item in document["registry_modules"]}
    ):
        lines.append(f"module {source}: removed")
    return lines or ["no pins changed"]


def generate(policy: dict[str, Any]) -> dict[str, Any]:
    """Resolve the policy into the manifest document the preparer consumes."""
    exact_keys(policy, {"schema", "providers", "registry_modules"}, "policy")
    if policy["schema"] != 1 or type(policy["schema"]) is not int:
        raise ValueError("policy schema must be the integer 1")
    providers = []
    for entry in policy["providers"]:
        exact_keys(entry, {"address", "constraint"}, "provider")
        providers.append(
            resolve_provider(
                full_address(entry["address"], "provider", modules=False), entry["constraint"]
            )
        )
    return {
        "schema": 1,
        "engine": "terraform",
        "providers": sorted(providers, key=lambda item: item["source"]),
        "modules": [],
        "registry_modules": resolve_modules(policy["registry_modules"]),
    }


def main() -> None:
    """Resolve, prove, and write; `--check` compares instead of writing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--check", action="store_true", help="fail instead of writing when the manifest differs"
    )
    args = parser.parse_args()
    document = generate(json.loads(args.policy.read_text(encoding="utf-8")))
    prep.checked_manifest(copy.deepcopy(document))
    dry_run(document)
    text = render(document) + "\n"
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        raise SystemExit(0 if current == text else "manifest differs from the policy resolution")
    previous = (
        json.loads(args.output.read_text(encoding="utf-8")) if args.output.is_file() else None
    )
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print("manifest written:", args.output)
    for line in pin_changes(previous, document):
        print(" ", line)


if __name__ == "__main__":
    main()
