"""Generate a prepared dependency manifest from a policy file; run via the CLI.

The policy names what a human decides: the engine, approved providers with version bounds,
registry modules with constraints, and optionally a catalog of modules chosen by namespace and
name prefix. This script resolves the rest against that engine's registry. Nested calls bake at
the newest release their constraint admits, and each root gets the provider versions the engine
would select for it. A catalog root that cannot be baked is left out and recorded with its
reason. Nothing is written before a dry run of the real preparer has proven every pin.
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import terraform_dependencies as prep  # noqa: E402

REGISTRY_HOSTS = {"terraform": "registry.terraform.io", "opentofu": "registry.opentofu.org"}
GITHUB_API = "https://api.github.com"
# The OpenTofu registry answers 403 to urllib's default agent, so every request names this one.
USER_AGENT = "maf-manifest-generation/1"
MAX_BODY = 64 * 1024 * 1024
ANY_RELEASE = ">= 0.0.0"
GIT_REF = re.compile(r"git::https://github\.com/([^/]+)/([^/?]+)\?ref=([0-9a-f]{40})\Z")
RELEASE = re.compile(r"\d+\.\d+\.\d+\Z")
SUMS_LINE = re.compile(r"([0-9a-f]{64})\s+\*?(.+)\Z")
GITHUB_RELEASE = re.compile(
    r"https://github\.com/([^/]+)/([^/]+)/releases/download/([^/]+)/([^/]+)\Z"
)

Key = tuple[str, str]


def http_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    """GET one bounded body, retrying transient failures the way the research probes do."""
    for attempt in range(5):
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
            with urlopen(request, timeout=120) as response:
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


def exact_keys(
    value: object, keys: set[str], what: str, optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    """Reject unknown or missing policy fields rather than guessing."""
    if not isinstance(value, dict) or not keys <= value.keys() <= keys | optional:
        raise ValueError(f"{what} must carry exactly {sorted(keys)}, optionally {sorted(optional)}")
    return value


def satisfies(version: str, constraint: str, context: str) -> bool:
    """Check one version against one Terraform constraint with the preparer's grammar."""
    try:
        return prep.satisfies(version, constraint, "policy-constraint")
    except prep.Refused:
        raise ValueError(f"{context}: invalid constraint {constraint!r}") from None


def release_key(version: str) -> tuple[int, ...]:
    """Order X.Y.Z releases numerically."""
    return tuple(int(part) for part in version.split("."))


def newest_release(versions: list[str], constraint: str, context: str) -> str:
    """Pick the newest X.Y.Z release the constraint admits; prereleases are never pins."""
    releases = [item for item in versions if RELEASE.fullmatch(item)]
    for version in sorted(releases, key=release_key, reverse=True):
        if satisfies(version, constraint, context):
            return version
    raise ValueError(f"{context}: no release satisfies {constraint!r} among {sorted(releases)}")


def full_address(value: str, what: str, *, modules: bool, host: str) -> str:
    """Require one full address on the engine's registry; modules carry three name segments."""
    tail = prep._REGISTRY_PACKAGE if modules else prep._NAME + "/" + prep._NAME
    if not isinstance(value, str) or not re.fullmatch(re.escape(host) + "/" + tail, value):
        raise ValueError(f"{what}: not a {host} address: {value!r}")
    return value


def provider_versions(address: str, host: str) -> list[str]:
    """List every X.Y.Z release the registry publishes for linux_amd64."""
    _, namespace, kind = address.split("/")
    body = http_json(f"https://{host}/v1/providers/{namespace}/{kind}/versions")
    return [
        item["version"]
        for item in body["versions"]
        if RELEASE.fullmatch(item["version"])
        and {"os": "linux", "arch": "amd64"} in item.get("platforms", [])
    ]


def module_versions(source: str, host: str) -> list[str]:
    """List every X.Y.Z release the registry publishes for one module."""
    namespace, name, system = source.split("/")[-3:]
    body = http_json(f"https://{host}/v1/modules/{namespace}/{name}/{system}/versions")
    return [
        item["version"]
        for item in body["modules"][0]["versions"]
        if RELEASE.fullmatch(item["version"])
    ]


def module_address(source: str, host: str) -> str:
    """Return the registry's own spelling of a module address."""
    namespace, name, system = source.split("/")[-3:]
    body = http_json(f"https://{host}/v1/modules/{namespace}/{name}/{system}")
    return f"{host}/{body['namespace']}/{body['name']}/{body['provider']}"


def catalog_sources(namespace: str, prefixes: list[str], host: str) -> list[str]:
    """List every module in a namespace whose name starts with one of the prefixes."""
    found: set[str] = set()
    offset = 0
    while True:
        body = http_json(
            f"https://{host}/v1/modules?namespace={namespace}&limit=100&offset={offset}"
        )
        for item in body["modules"]:
            if item["namespace"].casefold() == namespace.casefold() and item["name"].startswith(
                tuple(prefixes)
            ):
                found.add(f"{host}/{item['namespace']}/{item['name']}/{item['provider']}")
        offset = body["meta"].get("next_offset")
        if not body["modules"] or offset is None:
            return sorted(found)


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


def resolve_provider(address: str, version: str, host: str) -> dict[str, Any]:
    """Pin one provider release: digest cross-check, repository id when on GitHub."""
    _, namespace, kind = address.split("/")
    meta = http_json(
        f"https://{host}/v1/providers/{namespace}/{kind}/{version}/download/linux/amd64"
    )
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


def terraform_get(url: str) -> str:
    """Return the X-Terraform-Get location of one registry module download."""
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=120) as response:
        return response.headers.get("X-Terraform-Get", "")


def download_module(source: str, version: str, host: str) -> dict[str, Any]:
    """Resolve one registry module release to its commit archive."""
    namespace, name, system = source.split("/")[-3:]
    location = terraform_get(
        f"https://{host}/v1/modules/{namespace}/{name}/{system}/{version}/download"
    )
    match = GIT_REF.fullmatch(location)
    if match is None:
        raise ValueError(
            f"{source} {version}: registry download is not a pinned commit: {location!r}"
        )
    owner, repo, revision = match.groups()
    data = http_bytes(f"https://codeload.github.com/{owner}/{repo}/zip/{revision}")
    return {"owner": owner, "repo": repo, "revision": revision, "data": data}


def read_directory(data: bytes, prefix: str, directory: str, context: str) -> Any:
    """Read one package directory as preparation will: calls, requirements, implied names."""
    texts: list[tuple[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for item in archive.infolist():
            name = item.filename.removeprefix(prefix)
            if (
                name != item.filename
                and not item.is_dir()
                and (posixpath.dirname(name) or ".") == directory
                and not posixpath.basename(name).startswith(".")
                and name.endswith((".tf", ".tf.json"))
            ):
                try:
                    texts.append((posixpath.basename(name), archive.read(item).decode("utf-8")))
                except UnicodeDecodeError:
                    raise ValueError(f"{context}: {name} is not UTF-8") from None
    if not texts:
        raise ValueError(f"{context}: directory {directory} has no configuration files")
    try:
        return prep.directory_configuration(texts)
    except ValueError as error:
        raise ValueError(f"{context}: cannot read directory {directory}: {error}") from None


class Resolution:
    """Resolve one policy against the registry, caching every lookup for the run."""

    def __init__(self, policy: dict[str, Any]) -> None:
        exact_keys(
            policy,
            {"schema", "engine", "providers", "registry_modules"},
            "policy",
            frozenset({"catalog"}),
        )
        if policy["schema"] != 1 or type(policy["schema"]) is not int:
            raise ValueError("policy schema must be the integer 1")
        self.engine = policy["engine"]
        if self.engine not in REGISTRY_HOSTS:
            raise ValueError(f"policy engine must be one of {sorted(REGISTRY_HOSTS)}")
        self.host = REGISTRY_HOSTS[self.engine]
        # Preparation bakes registry modules for Terraform only, so a policy may not ask.
        if self.engine != "terraform" and (policy["registry_modules"] or "catalog" in policy):
            raise ValueError(f"registry modules cannot be baked for {self.engine}")
        self.bounds: dict[str, str] = {}
        for entry in policy["providers"]:
            exact_keys(entry, {"address", "constraint"}, "provider")
            address = full_address(entry["address"], "provider", modules=False, host=self.host)
            if address in self.bounds:
                raise ValueError(f"duplicate provider in policy: {address}")
            satisfies("0.0.0", entry["constraint"], address)
            self.bounds[address] = entry["constraint"]
        self.explicit: dict[str, dict[str, Any]] = {}
        for entry in policy["registry_modules"]:
            exact_keys(entry, {"source", "constraint"}, "registry module")
            address = full_address(entry["source"], "registry module", modules=True, host=self.host)
            if address.casefold() in self.explicit:
                raise ValueError(f"duplicate registry module in policy: {address}")
            self.explicit[address.casefold()] = entry
        self.catalog = policy.get("catalog")
        self.skipped: dict[str, str] = {}
        if self.catalog is not None:
            exact_keys(self.catalog, {"namespace", "prefixes"}, "catalog", frozenset({"exclude"}))
            if not re.fullmatch(prep._REGISTRY_PART, self.catalog["namespace"]) or not (
                self.catalog["prefixes"]
                and all(isinstance(item, str) and item for item in self.catalog["prefixes"])
            ):
                raise ValueError("catalog needs a namespace and at least one name prefix")
            for item in self.catalog.get("exclude", []):
                exact_keys(item, {"source", "reason"}, "catalog exclusion")
                address = full_address(
                    item["source"], "catalog exclusion", modules=True, host=self.host
                )
                self.skipped[address.casefold()] = item["reason"]
        self.versions: dict[str, list[str]] = {}
        self.addresses: dict[str, str] = {}
        self.releases: dict[str, list[str]] = {}
        self.pins: dict[tuple[str, str], dict[str, Any]] = {}
        self.archives: dict[Key, dict[str, Any]] = {}
        self.directories: dict[tuple[Key, str], Any] = {}
        self.edges: dict[tuple[Key, str, str], dict[str, Any]] = {}

    def listed(self, address: str) -> bool:
        """Whether the policy approves baking this module source."""
        if address.casefold() in self.explicit:
            return True
        if self.catalog is None:
            return False
        namespace, name, _ = address.split("/")[-3:]
        return namespace.casefold() == self.catalog["namespace"].casefold() and name.startswith(
            tuple(self.catalog["prefixes"])
        )

    def address(self, source: str) -> str:
        if source.casefold() not in self.addresses:
            self.addresses[source.casefold()] = module_address(source, self.host)
        return self.addresses[source.casefold()]

    def release(self, source: str, constraint: str) -> str:
        if source not in self.versions:
            self.versions[source] = module_versions(source, self.host)
        return newest_release(self.versions[source], constraint, source)

    def archive(self, key: Key) -> dict[str, Any]:
        if key not in self.archives:
            self.archives[key] = download_module(*key, self.host)
        return self.archives[key]

    def directory(self, key: Key, directory: str) -> Any:
        if (key, directory) not in self.directories:
            archive = self.archive(key)
            prefix = f"{archive['repo']}-{archive['revision']}/"
            self.directories[key, directory] = read_directory(
                archive["data"], prefix, directory, f"{key[0]} {key[1]}"
            )
        return self.directories[key, directory]

    def edge(self, key: Key, directory: str, label: str, arguments: Any) -> dict[str, Any]:
        """Resolve one call: a local directory, or a registry release and entry directory."""
        if (key, directory, label) in self.edges:
            return self.edges[key, directory, label]
        context = f"{key[0]} {key[1]}: module {label!r} in {directory}"
        if arguments is None:
            raise ValueError(f"{context} is declared twice")
        source = arguments.get("source")
        if source is None:
            raise ValueError(f"{context} has a dynamic source")
        if source.startswith(("./", "../")):
            target = posixpath.normpath(posixpath.join(directory, source))
            if target == ".." or target.startswith("../"):
                raise ValueError(f"{context} escapes the package")
            edge: dict[str, Any] = {"local": target}
        else:
            match = prep.runner._REGISTRY_SOURCE.fullmatch(source)
            if match is None or (match.group(1) or self.host).lower() != self.host:
                raise ValueError(f"{context} calls an unsupported source {source!r}")
            requested = f"{self.host}/{match.group(2)}"
            if not self.listed(requested):
                raise ValueError(f"{context} calls {requested}, which the policy does not list")
            subdir = match.group(3)
            if subdir is not None:
                try:
                    prep.relative_path(subdir)
                except prep.Refused:
                    raise ValueError(f"{context} calls an unclean subdirectory") from None
            constraint = arguments.get("version")
            if constraint is None:
                raise ValueError(f"{context} does not pin a version")
            target_source = self.address(requested)
            edge = {"registry": (target_source, self.release(target_source, constraint))}
            if subdir is not None:
                edge["dir"] = subdir
        self.edges[key, directory, label] = edge
        return edge

    def tree(self, key: Key, entry: str, loaded: dict[Key, set[str]]) -> None:
        """Load every directory Terraform reads for a call into this package directory."""
        pending = [entry]
        while pending:
            directory = pending.pop()
            if directory in loaded.setdefault(key, set()):
                continue
            loaded[key].add(directory)
            calls = self.directory(key, directory)[0]
            for label, arguments in sorted(calls.items()):
                edge = self.edge(key, directory, label, arguments)
                if "local" in edge:
                    pending.append(edge["local"])
                else:
                    self.tree(edge["registry"], edge.get("dir", "."), loaded)

    def pin(self, address: str, version: str) -> dict[str, Any]:
        if (address, version) not in self.pins:
            self.pins[address, version] = resolve_provider(address, version, self.host)
        return copy.deepcopy(self.pins[address, version])

    def provider(self, address: str) -> list[str]:
        if address not in self.releases:
            self.releases[address] = provider_versions(address, self.host)
        return self.releases[address]

    def root(self, key: Key) -> tuple[dict[Key, set[str]], dict[str, str]]:
        """Load one root and pick, per provider, the newest release every requirement admits."""
        loaded: dict[Key, set[str]] = {}
        self.tree(key, ".", loaded)
        needs: dict[str, list[str]] = {}
        for package, directories in loaded.items():
            for directory in directories:
                _, required, implied = self.directory(package, directory)
                try:
                    found = prep.provider_needs(required, implied)
                except prep.Refused:
                    raise ValueError(
                        f"{package[0]} {package[1]}: a provider requirement in {directory} "
                        "is not a literal"
                    ) from None
                for address, constraints in found.items():
                    needs.setdefault(address, []).extend(constraints)
        picks: dict[str, str] = {}
        for address, constraints in sorted(needs.items()):
            if address not in self.bounds:
                raise ValueError(f"needs provider {address}, which the policy does not approve")
            bound = [self.bounds[address], *sorted(set(constraints))]
            admitted = [
                version
                for version in self.provider(address)
                if all(satisfies(version, item, address) for item in bound)
            ]
            if not admitted:
                raise ValueError(f"no release of {address} satisfies {bound}")
            picks[address] = max(admitted, key=release_key)
        return loaded, picks

    def document(
        self, kept: dict[Key, tuple[dict[Key, set[str]], dict[str, str]]], excluded: list[Any]
    ) -> dict[str, Any]:
        """Build the manifest the kept roots need."""
        directories: dict[Key, set[str]] = {}
        pins: set[tuple[str, str]] = set()
        for loaded, picks in kept.values():
            for package, found in loaded.items():
                directories.setdefault(package, set()).update(found)
            pins.update(picks.items())
        for address, bound in self.bounds.items():
            pins.add((address, newest_release(self.provider(address), bound, address)))
        names: dict[Key, str] = {}
        for source, version in directories:
            name, system = source.split("/")[-2:]
            names[source, version] = f"{name}-{system}-{version}".lower()
        if len(set(names.values())) != len(names):
            raise ValueError("registry modules resolve to colliding names")
        modules = []
        for package in sorted(directories, key=lambda item: names[item]):
            graph: dict[str, dict[str, Any]] = {}
            for directory in sorted(directories[package]):
                graph[directory] = {}
                calls = self.directory(package, directory)[0]
                for label, arguments in sorted(calls.items()):
                    edge = dict(self.edge(package, directory, label, arguments))
                    if "registry" in edge:
                        edge["registry"] = names[edge["registry"]]
                    graph[directory][label] = edge
            archive = self.archive(package)
            modules.append(
                {
                    "name": names[package],
                    "source": package[0],
                    "version": package[1],
                    "revision": archive["revision"],
                    "graph": graph,
                    "artifact": {
                        "url": (
                            f"https://codeload.github.com/{archive['owner']}/{archive['repo']}"
                            f"/zip/{archive['revision']}"
                        ),
                        "sha256": hashlib.sha256(archive["data"]).hexdigest(),
                        "provenance": f"Registry {package[1]} download resolves to this commit",
                    },
                }
            )
        document: dict[str, Any] = {
            "schema": 1,
            "engine": self.engine,
            "providers": [
                self.pin(address, version)
                for address, version in sorted(
                    pins, key=lambda item: (item[0], release_key(item[1]))
                )
            ],
            "modules": [],
            "registry_modules": modules,
        }
        if excluded:
            document["excluded"] = sorted(
                excluded, key=lambda item: (item["source"], item["version"])
            )
        return document

    def refusals(self, document: dict[str, Any]) -> dict[str, str]:
        """Run the preparer's per-package checks on the cached archives; name what it refuses."""
        try:
            prep.checked_manifest(copy.deepcopy(document))
        except prep.Refused as error:
            raise ValueError(f"the manifest itself is refused: {error}") from None
        catalog = {item["name"]: item for item in document["registry_modules"]}
        keys = {
            item["name"]: (item["source"], item["version"]) for item in document["registry_modules"]
        }
        refused: dict[str, str] = {}
        sources: dict[str, Any] = {}
        for item in document["registry_modules"]:
            data = self.archive(keys[item["name"]])["data"]
            try:
                sources[item["name"]] = prep.registry_module_files(
                    item, data, catalog, document["providers"]
                )[1]
            except prep.Refused as error:
                refused[item["name"]] = str(error)
        if not refused:
            for item in document["registry_modules"]:
                for directory in item["graph"]:
                    try:
                        prep.registry_inventory(item["name"], directory, catalog, sources)
                    except prep.Refused as error:
                        refused[item["name"]] = str(error)
        return refused


def reason(error: Exception) -> str:
    """Bound an exclusion reason to the printable ASCII the manifest accepts."""
    return re.sub(r"[^\x20-\x7e]", "?", str(error))[:300] or "unknown"


def generate(policy: dict[str, Any]) -> dict[str, Any]:
    """Resolve the policy into the manifest document the preparer consumes."""
    resolution = Resolution(policy)
    required: list[Key] = []
    for entry in resolution.explicit.values():
        source = resolution.address(entry["source"])
        required.append((source, resolution.release(source, entry["constraint"])))
    optional: list[Key] = []
    excluded: list[dict[str, str]] = []
    if resolution.catalog is not None:
        for source in catalog_sources(
            resolution.catalog["namespace"], resolution.catalog["prefixes"], resolution.host
        ):
            if source.casefold() in resolution.explicit:
                continue
            try:
                version = resolution.release(source, ANY_RELEASE)
            except ValueError:
                continue
            if not re.fullmatch(re.escape(resolution.host) + "/" + prep._REGISTRY_PACKAGE, source):
                excluded.append(
                    {
                        "source": source,
                        "version": version,
                        "reason": "Terraform refuses this registry address",
                    }
                )
            elif source.casefold() in resolution.skipped:
                excluded.append(
                    {
                        "source": source,
                        "version": version,
                        "reason": resolution.skipped[source.casefold()],
                    }
                )
            else:
                optional.append((source, version))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(resolution.archive, sorted(set(required + optional))))
    kept: dict[Key, tuple[dict[Key, set[str]], dict[str, str]]] = {}
    for key in sorted(set(required)):
        kept[key] = resolution.root(key)
    for key in sorted(set(optional) - set(required)):
        try:
            kept[key] = resolution.root(key)
        except ValueError as error:
            excluded.append({"source": key[0], "version": key[1], "reason": reason(error)})
    while True:
        document = resolution.document(kept, excluded)
        refused = resolution.refusals(document)
        if not refused:
            return document
        names = {
            item["name"]: (item["source"], item["version"]) for item in document["registry_modules"]
        }
        before = len(kept)
        for key, (loaded, _) in list(kept.items()):
            blocked = sorted(name for name in refused if names[name] in loaded)
            if not blocked:
                continue
            message = f"preparation refuses {blocked[0]}: {refused[blocked[0]]}"
            if key in required:
                raise ValueError(f"{key[0]} {key[1]}: {message}")
            del kept[key]
            excluded.append({"source": key[0], "version": key[1], "reason": message})
        if len(kept) == before:
            raise ValueError(f"preparation refuses packages no root owns: {sorted(refused)}")


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
    for kind in ("providers", "registry_modules"):
        before = {(item["source"], item["version"]) for item in previous.get(kind, [])}
        after = {(item["source"], item["version"]) for item in document.get(kind, [])}
        label = "provider" if kind == "providers" else "module"
        lines += [
            f"{label} {source} {version}: added" for source, version in sorted(after - before)
        ]
        lines += [
            f"{label} {source} {version}: removed" for source, version in sorted(before - after)
        ]
    before = {(item["source"], item["version"]) for item in previous.get("excluded", [])}
    after = {(item["source"], item["version"]) for item in document.get("excluded", [])}
    lines += [f"excluded {source} {version}" for source, version in sorted(after - before)]
    lines += [
        f"no longer excluded {source} {version}" for source, version in sorted(before - after)
    ]
    return lines or ["no pins changed"]


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
