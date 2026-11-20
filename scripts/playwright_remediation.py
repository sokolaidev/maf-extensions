"""Map an inventoried Playwright-owned Node binary to a bounded Python package update."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from container_release import now, read, timestamp, write
from plan_go_remediation import evidence, locations, semver
from vendor_fix_evidence import NoRedirect

PROFILE = "document-export"
MANIFEST = "images/document-export/requirements.in"
LOCK = "images/document-export/requirements.txt"
INDEX = "https://pypi.org/pypi/playwright/json"
UPSTREAM = "https://raw.githubusercontent.com/microsoft/playwright-python/refs/tags/v"
LIMIT = 8 * 1024**2
BLOCK = re.compile(
    rb"(?m)^playwright==([0-9]+\.[0-9]+\.[0-9]+) \\\r?\n"
    rb"(?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\r?\n)+"
)


def version(value: str) -> tuple[int, ...]:
    """Require a stable, unambiguous upstream version."""
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value) is None:
        raise ValueError("Unsupported bundled-runtime version")
    return semver(value)


def pin(root: Path) -> str:
    """Require the editable requirement and build-consumed hash lock to agree."""
    manifest = (root / MANIFEST).read_text(encoding="utf-8")
    pins = re.findall(r"(?m)^playwright==([0-9]+\.[0-9]+\.[0-9]+)$", manifest)
    locks = list(BLOCK.finditer((root / LOCK).read_bytes()))
    declarations = re.findall(r"(?im)^\s*playwright\b[^\n]*", manifest)
    if (
        len(declarations) != 1
        or len(pins) != 1
        or len(locks) != 1
        or locks[0][1].decode() != pins[0]
    ):
        raise ValueError("Expected matching exact Playwright requirement and hash lock")
    version(pins[0])
    return pins[0]


def owner(sbom: dict[str, Any], component: dict[str, Any]) -> str | None:
    """Bind the binary to Python RECORD ownership and the same inventoried file digest."""
    if component.get("name") != "node" or component.get("type") != "binary":
        return None
    packages = [
        a for a in sbom["artifacts"] if a.get("name") == "playwright" and a.get("type") == "python"
    ]
    if len(packages) != 1:
        return None
    package = packages[0]
    metadata = package.get("metadata") or {}
    prefix = metadata.get("sitePackagesRootPath", "")
    if (
        not isinstance(prefix, str)
        or re.fullmatch(r"/usr/local/lib/python3\.[0-9]+/(?:dist|site)-packages", prefix) is None
    ):
        return None
    guest_path = f"{prefix}/playwright/driver/node"
    if locations(component) != [guest_path] or (
        metadata.get("name"),
        metadata.get("version"),
        package.get("purl"),
    ) != ("playwright", package["version"], f"pkg:pypi/playwright@{package['version']}"):
        return None
    if f"{prefix}/playwright-{package['version']}.dist-info/RECORD" not in locations(package):
        return None
    records = [f for f in metadata.get("files", []) if f.get("path") == "playwright/driver/node"]
    files = [f for f in sbom.get("files", []) if f.get("location", {}).get("path") == guest_path]
    if len(records) != 1 or len(files) != 1:
        return None
    declared = records[0].get("digest", {})
    digests = [
        d.get("value") for d in files[0].get("digests", []) if d.get("algorithm") == "sha256"
    ]
    if (
        declared.get("algorithm") != "sha256"
        or len(digests) != 1
        or re.fullmatch(r"[0-9a-f]{64}", digests[0] or "") is None
    ):
        return None
    encoded = base64.urlsafe_b64encode(bytes.fromhex(digests[0])).decode().rstrip("=")
    if declared.get("value") != encoded or str(files[0].get("metadata", {}).get("size")) != str(
        records[0].get("size")
    ):
        return None
    version(package["version"])
    return package["version"]


def needed(directories: list[Path]) -> bool:
    """Avoid acquiring package metadata for unrelated platform findings."""
    for directory in directories:
        identity, matches, _ = evidence(directory)
        if identity["profile"] == PROFILE:
            sbom = read(directory / "sbom.syft.json")
            if any(owner(sbom, match["artifact"]) is not None for match in matches):
                return True
    return False


def download(url: str) -> bytes:
    """Fetch only the reviewed package index or exact upstream version declaration."""
    if (
        url != INDEX
        and re.fullmatch(re.escape(UPSTREAM) + r"[0-9]+\.[0-9]+\.[0-9]+/NODE_VERSION", url) is None
    ):
        raise ValueError("Unapproved Playwright metadata URL")
    with urllib.request.build_opener(NoRedirect()).open(url, timeout=60) as response:
        raw = response.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError("Playwright metadata exceeds the size limit")
    return raw


def release(data: dict[str, Any], selected: str) -> list[str]:
    """Keep hashes for the published distributions and require a Linux/amd64 wheel."""
    version(selected)
    files = data["releases"].get(selected, [])
    if not files or any(f.get("yanked") is not False for f in files):
        raise ValueError("Playwright release is absent or yanked")
    hashes, names = [], []
    for item in files:
        name = item["filename"]
        wheel = re.fullmatch(
            rf"playwright-{re.escape(selected)}-py3-none-[a-zA-Z0-9_.]+\.whl", name
        )
        if not (
            (wheel and item["packagetype"] == "bdist_wheel")
            or (name == f"playwright-{selected}.tar.gz" and item["packagetype"] == "sdist")
        ):
            raise ValueError("Unexpected Playwright distribution")
        if (
            re.fullmatch(r"https://files\.pythonhosted\.org/packages/[a-zA-Z0-9/_.-]+", item["url"])
            is None
            or item["url"].rsplit("/", 1)[-1] != name
        ):
            raise ValueError("Unexpected Playwright archive origin")
        digest = item["digests"]["sha256"]
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("Invalid Playwright archive hash")
        hashes.append(digest)
        names.append(name)
    if len(set(names)) != len(names) or not any(
        re.fullmatch(
            rf"playwright-{re.escape(selected)}-py3-none-manylinux[0-9_.]*_x86_64\.whl", name
        )
        for name in names
    ):
        raise ValueError("Playwright release has no unambiguous Linux/amd64 wheel")
    return sorted(set(hashes))


def select(data: dict[str, Any], before: str) -> str:
    """Stay within the reviewed Playwright major/minor compatibility line."""
    if data.get("info", {}).get("name") != "playwright" or not isinstance(
        data.get("releases"), dict
    ):
        raise ValueError("PyPI metadata identifies another package")
    candidates = []
    for value in data["releases"]:
        try:
            parsed = version(value)
            if parsed[:2] == version(before)[:2] and parsed >= version(before):
                release(data, value)
                candidates.append((parsed, value))
        except (ValueError, KeyError, TypeError):
            continue
    if not candidates:
        raise ValueError("No supported Playwright release on the source compatibility line")
    return max(candidates)[1]


def acquire(root: Path, directory: Path) -> dict[str, Any]:
    """Retain package hashes and source/candidate Node declarations before planning edits."""
    directory.mkdir(parents=True, exist_ok=False)
    before = pin(root)
    raw = download(INDEX)
    after = select(json.loads(raw), before)
    documents = {"index.json": (INDEX, raw)}
    for name, value in (("source-node.txt", before), ("candidate-node.txt", after)):
        url = f"{UPSTREAM}{value}/NODE_VERSION"
        documents[name] = (url, download(url))
    receipt = {"fetchedAt": now(), "before": before, "sources": {}}
    for name, (url, content) in documents.items():
        (directory / name).write_bytes(content)
        receipt["sources"][name] = {"url": url, "sha256": hashlib.sha256(content).hexdigest()}
    write(directory / "receipt.json", receipt)
    return load(directory, before)


def load(directory: Path, before: str) -> dict[str, Any]:
    """Recompute package selection from bounded, fresh retained vendor bytes."""
    if directory.is_symlink() or (directory / "receipt.json").is_symlink():
        raise ValueError("Playwright evidence symlinks are not supported")
    receipt = read(directory / "receipt.json")
    if receipt["before"] != before or not timedelta(0) <= timestamp(now()) - timestamp(
        receipt["fetchedAt"]
    ) < timedelta(hours=48):
        raise ValueError("Playwright metadata is stale or identifies another source pin")
    if set(receipt["sources"]) != {"index.json", "source-node.txt", "candidate-node.txt"}:
        raise ValueError("Incomplete Playwright metadata")
    raw = {}
    for name, record in receipt["sources"].items():
        path = directory / name
        if path.is_symlink() or path.stat().st_size > LIMIT:
            raise ValueError("Invalid retained Playwright metadata")
        raw[name] = path.read_bytes()
        if hashlib.sha256(raw[name]).hexdigest() != record["sha256"]:
            raise ValueError("Playwright metadata hash changed")
    data = json.loads(raw["index.json"])
    after = select(data, before)
    urls = {
        "index.json": INDEX,
        "source-node.txt": f"{UPSTREAM}{before}/NODE_VERSION",
        "candidate-node.txt": f"{UPSTREAM}{after}/NODE_VERSION",
    }
    if any(receipt["sources"][name]["url"] != url for name, url in urls.items()):
        raise ValueError("Playwright metadata origin changed")
    source_node, candidate_node = (
        raw[name].decode().strip() for name in ("source-node.txt", "candidate-node.txt")
    )
    version(source_node)
    version(candidate_node)
    return {
        "receipt": receipt,
        "before": before,
        "after": after,
        "sourceNode": source_node,
        "candidateNode": candidate_node,
        "hashes": release(data, after),
    }


def plan(
    root: Path,
    sbom: dict[str, Any],
    component: dict[str, Any],
    fix: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Propose the owning package only when it declares a compatible Node security fix."""
    published = owner(sbom, component)
    if published is None:
        return {"status": "unmapped-bundled-runtime"}, None
    installed = version(component["version"])
    targets = []
    if fix.get("state") == "fixed":
        for value in fix.get("versions") or []:
            try:
                parsed = version(value)
                if parsed[0] == installed[0] and parsed > installed:
                    targets.append((parsed, value))
            except (ValueError, TypeError):
                continue
    if not targets or metadata is None:
        return {"status": "no-compatible-owner-fix"}, None
    fixed, target = min(targets)
    before = pin(root)
    if metadata["before"] != before or version(published) > version(before):
        raise ValueError("Playwright metadata or published owner differs from source policy")
    source, candidate = version(metadata["sourceNode"]), version(metadata["candidateNode"])
    row = {"target": target, "owner": "playwright", "ownerVersion": before}
    if source[0] == fixed[0] and source >= fixed:
        return row | {"status": "source-pin-sufficient"}, None
    if (
        candidate[0] != fixed[0]
        or candidate < fixed
        or version(metadata["after"]) <= version(before)
    ):
        return {"status": "no-compatible-owner-fix"}, None
    update = {
        "path": MANIFEST,
        "lock": LOCK,
        "component": "playwright",
        "type": "python",
        "before": before,
        "after": metadata["after"],
        "hashes": metadata["hashes"],
    }
    return row | {"status": "update", "ownerVersion": metadata["after"], "path": MANIFEST}, update


def proposal(root: Path, update: dict[str, Any]) -> dict[str, bytes]:
    """Change only the exact Playwright pin and its distribution hashes."""
    before, after = update["before"], update["after"]
    if (
        update["path"] != MANIFEST
        or update["lock"] != LOCK
        or update["component"] != "playwright"
        or pin(root) != before
        or version(after)[:2] != version(before)[:2]
        or version(after) <= version(before)
    ):
        raise ValueError("Unsafe bundled-runtime owner update")
    hashes = update["hashes"]
    if (
        not hashes
        or sorted(set(hashes)) != hashes
        or any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes)
    ):
        raise ValueError("Invalid Playwright lock hashes")
    raw = (root / LOCK).read_bytes()
    newline = b"\r\n" if b"\r\n" in raw else b"\n"
    lines = [f"playwright=={after} \\"] + [
        f"    --hash=sha256:{value}" + (" \\" if i < len(hashes) - 1 else "")
        for i, value in enumerate(hashes)
    ]
    locked, count = BLOCK.subn(
        lambda _: newline.join(line.encode() for line in lines) + newline, raw
    )
    if count != 1:
        raise ValueError("Ambiguous Playwright lock block")
    manifest = re.sub(
        rb"(?m)^playwright==" + re.escape(before.encode()) + rb"(?=\r?$)",
        f"playwright=={after}".encode(),
        (root / MANIFEST).read_bytes(),
        count=1,
    )
    return {MANIFEST: manifest, LOCK: locked}


def verify(sbom: dict[str, Any], row: dict[str, Any]) -> None:
    """Require both the expected Python owner and its fixed Node binary in the built image."""
    nodes = [
        a
        for a in sbom["artifacts"]
        if a.get("name") == "node"
        and a.get("type") == "binary"
        and locations(a) == row["guestPaths"]
    ]
    if len(nodes) != 1 or owner(sbom, nodes[0]) != row["ownerVersion"]:
        raise ValueError("Targeted bundled runtime ownership is absent from the rebuilt inventory")
    actual, target = version(nodes[0]["version"]), version(row["target"])
    if actual[0] != target[0] or actual < target:
        raise ValueError("Targeted bundled runtime fix is absent from the rebuilt inventory")
