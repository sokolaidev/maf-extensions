"""Measure what baking every latest Azure Verified Module and its dependencies would take.

    uv run python docs/sandbox/research/terraform-avm-catalog-probe.py --cache <dir> --output e.json

Reads public registry metadata, caches module archives and provider packages, and parses module
source with the preparer's helpers. It runs no Terraform or provider code and prepares no image.
Set GITHUB_TOKEN to also read whether each module commit carries a verified signature.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import posixpath
import re
import sys
import time
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import terraform_dependencies as prep  # noqa: E402

REGISTRY = "https://registry.terraform.io"
PREFIXES = ("avm-res-", "avm-ptn-", "avm-utl-")
SUBDIR = r"(?://([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*))?"
RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
TERM = re.compile(r"\s*(=|!=|>=|<=|>|<|~>)?\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*")


def fetch(url: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> Any:
    """Return the open response, retrying transient registry and CDN failures."""
    for attempt in range(5):
        try:
            return urlopen(Request(url, method=method, headers=headers or {}), timeout=120)
        except HTTPError as error:
            if error.code < 500 and error.code != 429:
                raise
        except OSError:
            pass
        time.sleep(2**attempt)
    raise RuntimeError(f"unavailable: {url}")


def get_json(url: str, headers: dict[str, str] | None = None) -> Any:
    """Read one JSON document."""
    with fetch(url, headers=headers) as response:
        return json.load(response)


def cached(url: str, path: Path) -> bytes:
    """Download once into the cache and reuse the bytes afterwards."""
    if not path.exists():
        with fetch(url) as response:
            data = response.read()
        path.write_bytes(data)
    return path.read_bytes()


def release(version: str) -> tuple[int, int, int] | None:
    """Parse a release version; prereleases and other forms return None."""
    match = RELEASE.fullmatch(version)
    return tuple(int(part) for part in match.groups()) if match else None  # type: ignore[return-value]


def matches(version: tuple[int, int, int], constraint: str | None, provider: bool) -> bool | None:
    """Module constraints follow go-version and provider constraints go-versions; None if unsure."""
    for term in (constraint or "").split(","):
        if not term.strip() and constraint is not None and constraint.strip():
            return None
        if not term.strip():
            continue
        match = TERM.fullmatch(term)
        if match is None:
            return None
        operator = match.group(1) or "="
        given = [int(part) for part in match.groups()[1:] if part is not None]
        bound = tuple(given + [0] * (3 - len(given)))
        if operator == "~>":
            keep = max(len(given) - 1, 0 if not provider else 1)
            upper = list(bound[:keep]) + [0] * (3 - keep)
            if keep:
                upper[keep - 1] += 1
            allowed = version >= bound and (keep == 0 or version < tuple(upper))
        else:
            allowed = {
                "=": version == bound,
                "!=": version != bound,
                ">": version > bound,
                ">=": version >= bound,
                "<": version < bound,
                "<=": version <= bound,
            }[operator]
        if not allowed:
            return False
    return True


def newest(versions: list[str], constraints: list[str | None], provider: bool) -> dict[str, Any]:
    """Pick the newest release every constraint admits, as registry resolution would."""
    unsure = False
    for version in sorted(versions, key=lambda item: release(item) or (0, 0, 0), reverse=True):
        parsed = release(version)
        if parsed is None:
            continue
        verdicts = [matches(parsed, constraint, provider) for constraint in constraints]
        unsure |= None in verdicts
        if all(verdicts):
            return {"version": version, "unsure": unsure}
    return {"version": None, "unsure": unsure}


def package_graph(data: bytes, prefix: str, entries: set[str]) -> dict[str, Any]:
    """Follow local calls from the root and called subdirectories; record calls and providers."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        files = {
            item.filename[len(prefix) :]: item
            for item in archive.infolist()
            if item.filename.startswith(prefix) and not item.is_dir()
        }
        graph: dict[str, dict[str, Any]] = {}
        calls: list[dict[str, Any]] = []
        providers: list[list[Any]] = []
        issues: set[str] = set()
        pending = [".", *sorted(entries)]
        while pending:
            directory = pending.pop()
            if directory in graph:
                continue
            graph[directory] = {}
            names = [n for n in files if (posixpath.dirname(n) or ".") == directory]
            if not any(n.endswith((".tf", ".tf.json")) for n in names):
                issues.add("empty-directory")
            declared: set[str] = set()
            implied: set[str] = set()
            for name in sorted(names):
                stem = posixpath.basename(name).removesuffix(".json")
                if stem.endswith((".tofu",)):
                    issues.add("tofu-file")
                if not stem.endswith(".tf"):
                    continue
                if stem.removesuffix(".tf") == "override" or stem.endswith("_override.tf"):
                    issues.add("override-file")
                text = archive.read(files[name]).decode("utf-8", errors="replace")
                json_syntax = name.endswith(".json")
                try:
                    parsed = json.loads(text) if json_syntax else prep.hcl2.loads(text)
                except Exception:
                    try:
                        parsed = prep.hcl2.loads(text.replace("\r\n", "\n"))
                        issues.add("parse-needs-lf")
                    except Exception:
                        issues.add("parse-error")
                        continue
                for label, block in prep._blocks(parsed, "module", json_syntax):
                    source = prep._literal(block.get("source"), json_syntax)
                    version = prep._literal(block.get("version"), json_syntax)
                    call = {"dir": directory, "label": label, "source": source}
                    if source is None:
                        call["kind"] = "dynamic"
                    elif source.startswith(("./", "../")):
                        target = posixpath.normpath(posixpath.join(directory, source))
                        call.update(kind="local", target=target)
                        if target.startswith("../") or target == "..":
                            issues.add("escape")
                        else:
                            graph[directory][label] = {"local": target}
                            pending.append(target)
                    elif found := re.fullmatch(
                        rf"(?:[^/]+/)?{prep._REGISTRY_PACKAGE}{SUBDIR}", source
                    ):
                        call.update(kind="registry", constraint=version, subdir=found.group(1))
                    else:
                        call["kind"] = "remote"
                    calls.append(call)
                for settings in prep._bodies(parsed, "terraform"):
                    for local, need in prep._blocks(settings, "required_providers", json_syntax):
                        source, constraint = f"hashicorp/{local}", need
                        if isinstance(need, dict):
                            source = prep._literal(need.get("source"), json_syntax) or source
                            constraint = need.get("version")
                        declared.add(local)
                        address = prep._provider_source(source)
                        if address:
                            providers.append(
                                [address, prep._literal(constraint, json_syntax), "declared"]
                            )
                for kind in ("resource", "data", "ephemeral"):
                    implied.update(
                        label.split("_")[0] for label, _ in prep._blocks(parsed, kind, json_syntax)
                    )
            for local in sorted(implied - declared - {"terraform"}):
                providers.append([f"{prep.REGISTRY_HOST}/hashicorp/{local}", None, "implied"])
        baked = [n for n in files if (posixpath.dirname(n) or ".") in graph]
        baked = [n for n in baked if not posixpath.basename(n).startswith(".")]
    return {
        "graph": graph,
        "calls": calls,
        "providers": providers,
        "issues": sorted(issues),
        "baked_files": len(baked),
        "baked_bytes": sum(files[n].file_size for n in baked),
        "archive_files": len(files),
        "archive_bytes_expanded": sum(item.file_size for item in files.values()),
    }


def module_versions(source: str) -> list[str]:
    """List every published version of one registry module."""
    namespace, name, system = source.split("/")[-3:]
    data = get_json(f"{REGISTRY}/v1/modules/{namespace}/{name}/{system}/versions")
    return [item["version"] for item in data["modules"][0]["versions"]]


def resolve(source: str, version: str, cache: Path, token: str | None) -> dict[str, Any]:
    """Resolve one module version to its commit archive; parsing happens later, on one thread."""
    namespace, name, system = source.split("/")[-3:]
    download = f"{REGISTRY}/v1/modules/{namespace}/{name}/{system}/{version}/download"
    with fetch(download) as response:
        location = response.headers.get("X-Terraform-Get", "")
    found = re.fullmatch(r"git::https://github\.com/([^/]+)/([^/?]+)\?ref=(.+)", location)
    record: dict[str, Any] = {"source": source, "version": version, "location_kind": "other"}
    if found is None:
        return record
    owner, repository, ref = found.groups()
    record.update(location_kind="git-commit" if re.fullmatch(r"[0-9a-f]{40}", ref) else "git-ref")
    record.update(repository=f"{owner}/{repository}", revision=ref)
    url = f"https://codeload.github.com/{owner}/{repository}/zip/{ref}"
    data = cached(url, cache / f"{repository}-{ref}.zip")
    record.update(archive_bytes=len(data), archive_sha256=hashlib.sha256(data).hexdigest())
    if token and record["location_kind"] == "git-commit":
        commit = get_json(
            f"https://api.github.com/repos/{owner}/{repository}/commits/{ref}",
            {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        record["commit_verified"] = commit["commit"]["verification"]["verified"]
    return record


def analyse(record: dict[str, Any], cache: Path, entries: set[str]) -> None:
    """Apply the archive policy and read the module graph; the HCL parser is not thread-safe."""
    if "repository" not in record:
        return
    stem = f"{record['repository'].split('/')[1]}-{record['revision']}"
    data = (cache / f"{stem}.zip").read_bytes()
    record["data"] = data
    try:
        prep.zip_files(data, limit=prep.MAX_TEXT)
        record["archive_policy"] = "accepted"
    except prep.Refused as refusal:
        record["archive_policy"] = str(refusal)
    record.update(package_graph(data, f"{stem}/", entries))


def safe_resolve(source: str, version: str, cache: Path, token: str | None) -> dict[str, Any]:
    """Record a resolution failure instead of abandoning the whole catalog."""
    try:
        return resolve(source, version, cache, token)
    except Exception as error:
        return {"source": source, "version": version, "location_kind": f"error:{error}"}


def provider_versions(address: str) -> list[str]:
    """List provider versions published for linux_amd64."""
    host, namespace, kind = address.split("/")
    if host != prep.REGISTRY_HOST:
        return []
    try:
        data = get_json(f"{REGISTRY}/v1/providers/{namespace}/{kind}/versions")
    except HTTPError as error:
        if error.code == 404:
            return []
        raise
    return [
        item["version"]
        for item in data["versions"]
        if {"os": "linux", "arch": "amd64"} in item.get("platforms", [])
    ]


def provider_package(address: str, version: str, cache: Path) -> dict[str, Any]:
    """Download one provider package and measure its packed and unpacked size."""
    _, namespace, kind = address.split("/")
    meta = get_json(f"{REGISTRY}/v1/providers/{namespace}/{kind}/{version}/download/linux/amd64")
    data = cached(meta["download_url"], cache / meta["filename"])
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        unpacked = sum(item.file_size for item in archive.infolist())
    return {
        "address": address,
        "version": version,
        "zip_bytes": len(data),
        "unpacked_bytes": unpacked,
        "sha256_matches_registry": hashlib.sha256(data).hexdigest() == meta["shasum"],
        "download_host": meta["download_url"].split("/")[2],
    }


def dependency_key(source: str) -> str | None:
    """Normalize a registry source to its full address, or None for another host."""
    match = re.fullmatch(rf"(?:([^/]+)/)?({prep._REGISTRY_PACKAGE}){SUBDIR}", source)
    if match is None or (match.group(1) or prep.REGISTRY_HOST).lower() != prep.REGISTRY_HOST:
        return None
    return f"{prep.REGISTRY_HOST}/{match.group(2)}"


def preparer_decision(record: dict[str, Any], packages: dict[tuple[str, str], Any]) -> str:
    """Run the current preparer on one package with its resolved dependencies as the catalog."""
    if "graph" not in record:
        return "not-github"
    name = "package"
    catalog = {name: {"name": name, "source": record["source"], "version": record["version"]}}
    graph = {directory: dict(edges) for directory, edges in record["graph"].items()}
    for call in record["calls"]:
        if call["kind"] != "registry" or not call.get("resolved"):
            continue
        key = (call["dependency"], call["resolved"])
        dependency = f"dep-{len(catalog)}"
        if any(item["source"].casefold() == key[0].casefold() for item in catalog.values()):
            existing = [
                n for n, i in catalog.items() if i["source"].casefold() == key[0].casefold()
            ]
            if catalog[existing[0]]["version"] != key[1]:
                return "two-versions-of-one-module"
            dependency = existing[0]
        catalog.setdefault(dependency, {"name": dependency, "source": key[0], "version": key[1]})
        graph[call["dir"]][call["label"]] = {"registry": dependency}
    providers = [
        {"source": address, "version": version}
        for address, version in record.get("provider_pins", {}).items()
        if version
    ]
    module = {
        "name": name,
        "revision": record["revision"],
        "graph": graph,
        "artifact": {
            "url": f"https://codeload.github.com/{record['repository']}/zip/{record['revision']}"
        },
    }
    try:
        prep.registry_module_files(
            module, packages[record["source"], record["version"]]["data"], catalog, providers
        )
        return "accepted"
    except prep.Refused as refusal:
        return str(refusal)
    except Exception as error:
        return f"error:{type(error).__name__}"


def main() -> None:
    """Resolve the catalog, measure it, and write the evidence file."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    token = os.environ.get("GITHUB_TOKEN")
    measured = datetime.now(UTC)
    listed: dict[str, Any] = {}
    offset: int | None = 0
    while offset is not None:
        page = get_json(f"{REGISTRY}/v1/modules?namespace=Azure&limit=100&offset={offset}")
        for item in page["modules"]:
            if item["name"].startswith(PREFIXES):
                listed[
                    f"{prep.REGISTRY_HOST}/{item['namespace']}/{item['name']}/{item['provider']}"
                ] = item
        offset = page["meta"].get("next_offset")
    pool = ThreadPoolExecutor(8)
    versions = dict(zip(listed, pool.map(module_versions, listed), strict=True))
    roots = {source: newest(found, [None], False)["version"] for source, found in versions.items()}
    spelling = {source.casefold(): source for source in listed}
    packages: dict[tuple[str, str], dict[str, Any]] = {}
    entries: dict[tuple[str, str], set[str]] = {}
    pending = [(source, version) for source, version in roots.items() if version]
    reread: list[dict[str, Any]] = []
    while pending or reread:
        batch = [key for key in dict.fromkeys(pending) if key not in packages]
        pending = []
        fetched = list(pool.map(lambda key: safe_resolve(*key, args.cache, token), batch))
        for record in fetched + reread:
            key = (record["source"], record["version"])
            packages[key] = record
            analyse(record, args.cache, entries.setdefault(key, set()))
            for call in record.get("calls", []):
                if call["kind"] != "registry":
                    continue
                dependency = dependency_key(call["source"])
                call["dependency"] = dependency
                if dependency is None:
                    continue
                source = spelling.setdefault(dependency.casefold(), dependency)
                if source not in versions:
                    versions[source] = module_versions(source)
                pick = newest(versions[source], [call["constraint"]], False)
                call.update(dependency=source, resolved=pick["version"], unsure=pick["unsure"])
                if pick["version"]:
                    pending.append((source, pick["version"]))
                    if call["subdir"]:
                        entries.setdefault((source, pick["version"]), set()).add(call["subdir"])
        # A subdirectory call can make more of an already-read package reachable.
        reread = [
            record
            for key, record in packages.items()
            if "graph" in record and not entries.get(key, set()) <= record["graph"].keys()
        ]

    def closure(key: tuple[str, str], seen: set[tuple[str, str]]) -> set[tuple[str, str]]:
        if key in seen or key not in packages:
            return seen
        seen.add(key)
        for call in packages[key].get("calls", []):
            if call.get("resolved"):
                closure((call["dependency"], call["resolved"]), seen)
        return seen

    def inventory(key: tuple[str, str], directory: str, depth: int) -> int:
        record = packages.get(key, {})
        if depth > 32 or directory not in record.get("graph", {}):
            return 0
        count = 0
        for call in record["calls"]:
            if call["dir"] != directory:
                continue
            if call["kind"] == "local" and call["target"] in record["graph"]:
                count += 1 + inventory(key, call["target"], depth + 1)
            elif call.get("resolved"):
                entry = call.get("subdir") or "."
                count += 1 + inventory((call["dependency"], call["resolved"]), entry, depth + 1)
        return count

    releases: dict[str, list[str]] = {}
    unknown_implied: set[str] = set()
    joint: dict[str, list[str | None]] = {}
    needed: set[tuple[str, str]] = set()
    rows = []
    for source, version in sorted(roots.items()):
        row: dict[str, Any] = {"source": source, "version": version}
        rows.append(row)
        if version is None:
            continue
        members = closure((source, version), set())
        constraints: dict[str, list[str | None]] = {}
        for member in members:
            for address, constraint, origin in packages[member].get("providers", []):
                if address not in releases:
                    releases[address] = provider_versions(address)
                if origin == "implied" and not releases[address]:
                    unknown_implied.add(address)
                    continue
                constraints.setdefault(address, []).append(constraint)
                joint.setdefault(address, []).append(constraint)
        pins = {}
        for address, found in sorted(constraints.items()):
            pick = newest(releases[address], found, True)
            pins[address] = pick["version"]
            if pick["version"]:
                needed.add((address, pick["version"]))
        row.update(
            packages=len(members),
            modules=sorted({member[0] for member in members} - {source}),
            providers=pins,
            inventory_records=inventory((source, version), ".", 0),
            published_at=listed[source].get("published_at"),
        )
        for member in members:
            packages[member].setdefault("provider_pins", {}).update(pins)
    for record in packages.values():
        record["preparer"] = preparer_decision(record, packages)
    providers = list(pool.map(lambda key: provider_package(*key, args.cache), sorted(needed)))
    for address, found in joint.items():
        joint[address] = [newest(releases[address], found, True)["version"]]
    count = lambda values: dict(sorted(Counter(values).items()))  # noqa: E731
    by_source: dict[str, set[str]] = {}
    for source, version in packages:
        by_source.setdefault(source, set()).add(version)
    recent = measured - timedelta(days=30)
    summary = {
        "listed_modules": len(listed),
        "by_kind": count(source.split("/")[2].split("-")[1] for source in listed),
        "with_release": sum(1 for version in roots.values() if version),
        "published_last_30_days": sum(
            1
            for item in listed.values()
            if item.get("published_at")
            and datetime.fromisoformat(item["published_at"].replace("Z", "+00:00")) > recent
        ),
        "packages_in_closure": len(packages),
        "sources_needing_several_versions": {
            source: sorted(found) for source, found in by_source.items() if len(found) > 1
        },
        "non_avm_dependencies": sorted(
            {source for source, _ in packages if not source.split("/")[2].startswith(PREFIXES)}
        ),
        "location_kinds": count(record["location_kind"] for record in packages.values()),
        "commit_verified": count(
            str(record.get("commit_verified")) for record in packages.values()
        ),
        "archive_policy": count(
            record.get("archive_policy", "none") for record in packages.values()
        ),
        "preparer_decisions": count(record["preparer"] for record in packages.values()),
        "issues": count(
            issue for record in packages.values() for issue in record.get("issues", [])
        ),
        "call_kinds": count(
            call["kind"] for record in packages.values() for call in record.get("calls", [])
        ),
        "unresolved_registry_calls": sum(
            1
            for record in packages.values()
            for call in record.get("calls", [])
            if call["kind"] == "registry" and not call.get("resolved")
        ),
        "unsure_constraints": sum(
            1
            for record in packages.values()
            for call in record.get("calls", [])
            if call.get("unsure")
        ),
        "archive_bytes": sum(record.get("archive_bytes", 0) for record in packages.values()),
        "baked_bytes": sum(record.get("baked_bytes", 0) for record in packages.values()),
        "baked_files": sum(record.get("baked_files", 0) for record in packages.values()),
        "max_graph_directories": max(len(r.get("graph", {})) for r in packages.values()),
        "max_inventory_records": max(row.get("inventory_records", 0) for row in rows),
        "roots_over_inventory_limit": sum(
            1 for row in rows if row.get("inventory_records", 0) > prep.MAX_INVENTORY
        ),
        "roots_with_provider_conflict": sorted(
            row["source"] for row in rows if None in row.get("providers", {}).values()
        ),
        "implied_providers_not_in_registry": sorted(unknown_implied),
        "provider_versions": count(item["address"] for item in providers),
        "provider_zip_bytes": sum(item["zip_bytes"] for item in providers),
        "provider_unpacked_bytes": sum(item["unpacked_bytes"] for item in providers),
        "single_version_per_provider": {address: pick[0] for address, pick in joint.items()},
        "nested_registry_pins": count(
            "exact"
            if re.fullmatch(r"\s*=?\s*\d+\.\d+\.\d+\s*", call["constraint"] or "")
            else "range"
            for record in packages.values()
            for call in record.get("calls", [])
            if call["kind"] == "registry"
        ),
    }
    sizes = {(item["address"], item["version"]): item["zip_bytes"] for item in providers}
    for row in rows:
        if row["version"] is None:
            continue
        members = closure((row["source"], row["version"]), set())
        blockers = {packages[member]["preparer"] for member in members} - {"accepted"}
        if any(call.get("subdir") for m in members for call in packages[m].get("calls", [])):
            blockers.add("subdirectory-call")
        if len({member[0] for member in members}) < len(members):
            blockers.add("several-versions-of-one-module")
        if len(members) > 16:
            blockers.add("more-than-16-packages")
        download = sum(packages[m].get("archive_bytes", 0) for m in members) + sum(
            sizes.get((address, version), 0) for address, version in row["providers"].items()
        )
        if download > prep.MAX_TOTAL:
            blockers.add("total-size")
        row["blockers_today"] = sorted(blockers)
    summary["roots_bakeable_alone_today"] = sum(1 for row in rows if not row.get("blockers_today"))
    summary["root_blockers"] = count(b for row in rows for b in row.get("blockers_today", []))
    compact = []
    for record in sorted(packages.values(), key=lambda r: (r["source"], r["version"])):
        compact.append(
            {
                key: record[key]
                for key in (
                    "source",
                    "version",
                    "repository",
                    "revision",
                    "archive_bytes",
                    "archive_sha256",
                    "baked_files",
                    "baked_bytes",
                    "archive_policy",
                    "preparer",
                    "issues",
                    "commit_verified",
                )
                if key in record
            }
            | {
                "dependencies": sorted(
                    f"{call['source']}@{call.get('resolved')}"
                    for call in record.get("calls", [])
                    if call["kind"] == "registry"
                )
            }
        )
    for row in rows:
        row.pop("modules", None)
    evidence = {
        "measured_at": measured.isoformat(timespec="seconds"),
        "registry": REGISTRY,
        "summary": summary,
        "roots": rows,
        "packages": compact,
        "providers": providers,
    }
    lines = []
    for key, value in evidence.items():
        if isinstance(value, list):
            rows = ",\n".join(f"  {json.dumps(row, sort_keys=True)}" for row in value)
            lines.append(f' "{key}": [\n{rows}\n ]')
        else:
            text = json.dumps(value, indent=1, sort_keys=True).replace("\n", "\n ")
            lines.append(f' "{key}": {text}')
    args.output.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
