"""Prepare host-approved ZIP dependencies; run via the CLI for a hard wall-time bound."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import ipaddress
import json
import os
import posixpath
import re
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qsl, urlsplit

import hcl2

MAX_MANIFEST = 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024
MAX_PROVIDER_EXPANDED = 1024 * 1024 * 1024
MAX_RESPONSE_HEAD = 32 * 1024
MAX_INVENTORY = 256
DEADLINE = 180
REGISTRY_HOST = "registry.terraform.io"
_NAME = r"[a-z0-9][a-z0-9_-]{0,63}"
_LABEL = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
_REGISTRY_PART = r"[0-9A-Za-z](?:[0-9A-Za-z_-]{0,62}[0-9A-Za-z])?"
_REGISTRY_PACKAGE = rf"{_REGISTRY_PART}/{_REGISTRY_PART}/[0-9a-z]{{1,64}}"
_RELEASE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
_CONSTRAINT = r"\s*(=|!=|>=|<=|>|<|~>)?\s*((?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2})\s*"
_DIGEST = r"[0-9a-f]{64}"
_NUMBER = r"(?:0|[1-9][0-9]*)"
_PRERELEASE = rf"(?:{_NUMBER}|[0-9]*[a-z-][a-z0-9-]*)"
_VERSION = rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:-{_PRERELEASE}(?:\.{_PRERELEASE})*)?"
_QUERY_KEYS = frozenset(
    "sp sv sr spr se rscd rsct skoid sktid skt ske sks skv sig jwt "
    "response-content-disposition response-content-type".split()
)


# Worker exit codes carry only fixed policy decisions; arbitrary diagnostics stay private.
_WORKER_DECISIONS = (
    "archive-collision archive-empty archive-encryption archive-entries archive-expansion "
    "archive-size archive-special archive-type artifact-digest artifact-mismatch deadline "
    "dns-answers dns-private dns-transition download-incomplete download-size engine "
    "file-path file-segments github-policy github-source manifest-duplicate "
    "manifest-fields manifest-object manifest-required manifest-schema manifest-size "
    "module-conflict module-cycle module-duplicate module-edge-name module-edge-target "
    "module-edges module-engine module-files module-graph module-graph-mismatch "
    "module-hidden module-json-duplicate module-name module-override module-precedence "
    "module-remote module-revision module-source module-state module-text "
    "module-unreachable modules output-exists provenance provider-conflict "
    "provider-platform provider-source provider-version providers redirect-artifact "
    "redirect-host redirect-limit redirect-unapproved registry-conflict registry-constraint "
    "registry-directory registry-edge registry-engine registry-host registry-inventory "
    "registry-modules registry-provider registry-revision registry-source registry-version "
    "response-encoding response-headers "
    "response-length response-status signed-content signed-fields signed-host signed-query "
    "total-size transfer-failed url-ascii url-authority url-fragment url-path url-query "
    "url-segments url-shape "
).split()


class Refused(ValueError):
    """A bounded policy decision safe to include in diagnostics."""


def require(condition: object, decision: str) -> None:
    """Keep input and upstream content out of exception messages."""
    if not condition:
        raise Refused(decision)


def exact_keys(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    """Reject unknown fields rather than silently dropping policy."""
    require(isinstance(value, dict), "manifest-object")
    require(required <= value.keys() <= required | (optional or set()), "manifest-fields")


def unique_json(data: str | bytes, decision: str) -> Any:
    """Refuse ambiguous objects before checking policy or module graph semantics."""

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, decision)
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=pairs)


def canonical_url(url: str, *, signed: bool = False) -> tuple[str, str]:
    """Accept only one spelling of an HTTPS request destination."""
    require(isinstance(url, str) and len(url) <= 16384, "url-shape")
    require(re.fullmatch(r"[\x21-\x7e]+", url), "url-ascii")
    parsed = urlsplit(url)
    host = parsed.netloc
    require(
        parsed.scheme == "https"
        and re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*\.[a-z]{2,63}", host),
        "url-authority",
    )
    require(not parsed.fragment and "#" not in url, "url-fragment")
    require(re.fullmatch(r"/[A-Za-z0-9._~/-]+", parsed.path), "url-path")
    require(
        all(part not in {"", ".", ".."} for part in parsed.path[1:].split("/")),
        "url-segments",
    )
    if signed:
        require(host == "release-assets.githubusercontent.com", "signed-host")
        require(0 < len(parsed.query) <= 8192, "signed-query")
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        keys = [key for key, _ in pairs]
        require(len(keys) == len(set(keys)) and set(keys) <= _QUERY_KEYS, "signed-fields")
        require("sig" in keys and "jwt" in keys, "signed-fields")
        require(
            all(value and all(32 <= ord(char) < 127 for char in value) for _, value in pairs),
            "signed-content",
        )
    else:
        require("?" not in url, "url-query")
    return host, parsed.path + ("?" + parsed.query if signed else "")


def relative_path(value: str, *, dot: bool = False) -> str:
    """Use a portable namespace without Windows aliases or archive traversal."""
    if dot and value == ".":
        return value
    require(isinstance(value, str) and len(value) <= 240, "file-path")
    require(re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value), "file-path")
    require(
        all(
            part not in {".", ".."}
            and not part.endswith(".")
            and part.split(".")[0].upper()
            not in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(10)),
                *(f"LPT{i}" for i in range(10)),
            }
            for part in value.split("/")
        ),
        "file-segments",
    )
    return value


def artifact_policy(value: Any) -> None:
    """Validate the complete host authority before making any network request."""
    exact_keys(value, {"url", "sha256", "provenance"}, {"redirects", "github_repository_id"})
    host, path = canonical_url(value["url"])
    require(re.fullmatch(_DIGEST, value["sha256"]), "artifact-digest")
    require(re.fullmatch(r"[A-Za-z0-9 ._:/@+-]{1,200}", value["provenance"]), "provenance")
    redirects = value.get("redirects", [])
    require(isinstance(redirects, list) and len(redirects) <= 3, "redirect-limit")
    for url in redirects:
        canonical_url(url)
    repository = value.get("github_repository_id")
    if repository is not None:
        require(not redirects and re.fullmatch(r"[1-9][0-9]{0,19}", repository), "github-policy")
        require(
            host == "github.com"
            and re.fullmatch(
                r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[^/]+/[^/]+", path
            ),
            "github-source",
        )


def checked_registry_modules(modules: Any, engine: str) -> None:
    """Pin each registry package to a GitHub commit archive and a declared, acyclic graph."""
    require(isinstance(modules, list) and len(modules) <= 16, "registry-modules")
    require(not modules or engine == "terraform", "registry-engine")
    targets: dict[str, set[str]] = {}
    sources: set[str] = set()
    for module in modules:
        exact_keys(module, {"name", "source", "version", "revision", "graph", "artifact"})
        name, source = module["name"], module["source"]
        require(isinstance(name, str) and re.fullmatch(_NAME, name), "module-name")
        require(isinstance(source, str), "registry-source")
        require(
            re.fullmatch(re.escape(REGISTRY_HOST) + "/" + _REGISTRY_PACKAGE, source),
            "registry-source",
        )
        require(name not in targets and source.casefold() not in sources, "registry-conflict")
        sources.add(source.casefold())
        version, revision = module["version"], module["revision"]
        require(isinstance(version, str) and re.fullmatch(_RELEASE, version), "registry-version")
        require(
            isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision),
            "registry-revision",
        )
        artifact_policy(module["artifact"])
        require(
            module["artifact"].keys() == {"url", "sha256", "provenance"}
            and re.fullmatch(
                r"https://codeload\.github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/zip/" + revision,
                module["artifact"]["url"],
            ),
            "registry-revision",
        )
        graph = module["graph"]
        require(isinstance(graph, dict) and "." in graph and len(graph) <= 64, "module-graph")
        targets[name] = set()
        for directory, edges in graph.items():
            relative_path(directory, dot=True)
            require(
                directory == "." or not any(part.startswith(".") for part in directory.split("/")),
                "module-hidden",
            )
            require(isinstance(edges, dict) and len(edges) <= 64, "module-edges")
            for label, edge in edges.items():
                require(re.fullmatch(_LABEL, label), "module-edge-name")
                require(isinstance(edge, dict) and len(edge) == 1, "registry-edge")
                if "local" in edge:
                    require(edge["local"] in graph, "module-edge-target")
                else:
                    exact_keys(edge, {"registry"})
                    require(isinstance(edge["registry"], str), "registry-edge")
                    targets[name].add(edge["registry"])
    visited: set[str] = set()

    def visit(name: str, ancestors: set[str]) -> None:
        require(name in targets, "registry-edge")
        require(name not in ancestors, "module-cycle")
        if name not in visited:
            for target in targets[name]:
                visit(target, ancestors | {name})
            visited.add(name)

    for name in targets:
        visit(name, set())


def checked_manifest(value: Any) -> dict[str, Any]:
    """Bind provider identities and module graphs to a single engine policy."""
    exact_keys(value, {"schema", "engine", "providers", "modules"}, {"registry_modules"})
    require(value["schema"] == 1 and type(value["schema"]) is int, "manifest-schema")
    require(value["engine"] in {"terraform", "opentofu"}, "engine")
    require(isinstance(value["providers"], list) and len(value["providers"]) <= 32, "providers")
    require(isinstance(value["modules"], list) and len(value["modules"]) <= 16, "modules")
    identities: set[str] = set()
    for provider in value["providers"]:
        exact_keys(provider, {"source", "version", "platform", "artifact"})
        source = provider["source"]
        require(re.fullmatch(r"[a-z0-9.-]+/" + _NAME + "/" + _NAME, source), "provider-source")
        canonical_url("https://" + source)
        require(
            re.fullmatch(_VERSION, provider["version"]),
            "provider-version",
        )
        require(provider["platform"] == "linux_amd64", "provider-platform")
        identity = f"{source}/{provider['version']}/{provider['platform']}"
        require(identity not in identities, "provider-conflict")
        identities.add(identity)
        artifact_policy(provider["artifact"])
    identities.clear()
    for module in value["modules"]:
        exact_keys(module, {"name", "revision", "subdir", "graph", "artifact"})
        require(re.fullmatch(_NAME, module["name"]), "module-name")
        require(module["name"] not in identities, "module-conflict")
        identities.add(module["name"])
        require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", module["revision"]), "module-revision")
        relative_path(module["subdir"])
        graph = module["graph"]
        require(isinstance(graph, dict) and "." in graph and len(graph) <= 64, "module-graph")
        for directory, edges in graph.items():
            relative_path(directory, dot=True)
            require(isinstance(edges, dict) and len(edges) <= 64, "module-edges")
            for name, target in edges.items():
                require(re.fullmatch(_NAME, name), "module-edge-name")
                require(target in graph, "module-edge-target")
        artifact_policy(module["artifact"])
    checked_registry_modules(value.get("registry_modules", []), value["engine"])
    return value


def remaining(deadline: float) -> float:
    """Use one deadline for all connections and reads."""
    left = deadline - time.monotonic()
    require(left > 0, "deadline")
    return left


def public_addresses(host: str) -> list[tuple[int, tuple[Any, ...]]]:
    """Reject mixed public/private DNS answers and pin the address used by TLS."""
    records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    require(0 < len(records) <= 32, "dns-answers")
    addresses = []
    for family, _, _, _, address in records:
        ip = ipaddress.ip_address(address[0])
        require(ip.is_global and not ip.is_multicast, "dns-private")
        if isinstance(ip, ipaddress.IPv6Address):
            require(ip in ipaddress.ip_network("2000::/3"), "dns-transition")
            require(
                ip.ipv4_mapped is None and ip.sixtofour is None and ip.teredo is None,
                "dns-transition",
            )
        addresses.append((family, address))
    return addresses


class _HeadReader:
    """Bound raw status and header bytes before the HTTP parser retains them."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.available = MAX_RESPONSE_HEAD

    def readline(self, size: int = -1) -> bytes:
        limit = self.available + 1
        data = self.stream.readline(min(size, limit) if size >= 0 else limit)
        self.available -= len(data)
        require(self.available >= 0, "response-headers")
        return data


class BoundedResponse(http.client.HTTPResponse):
    """Share one response-head budget across status, headers and interim responses."""

    def begin(self) -> None:
        stream = self.fp
        try:
            self.fp = cast(io.BufferedReader, _HeadReader(stream))
            super().begin()
        finally:
            # Body reads and failure cleanup retain the original buffered stream.
            self.fp = stream


class PinnedHTTPS(http.client.HTTPSConnection):
    """Connect directly to a checked IP, retaining certificate and SNI hostname checks."""

    response_class = BoundedResponse

    def __init__(self, host: str, deadline: float) -> None:
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=remaining(deadline), context=self.tls_context)
        self.deadline = deadline

    def connect(self) -> None:
        """Avoid proxies, CONNECT tunnels and a second DNS resolution."""
        addresses = public_addresses(self.host)
        for index, (family, address) in enumerate(addresses):
            # Reserve time for the other validated addresses if this route stalls.
            attempt_deadline = time.monotonic() + remaining(self.deadline) / (
                len(addresses) - index
            )
            raw = None
            try:
                raw = socket.socket(family, socket.SOCK_STREAM)
                raw.settimeout(remaining(attempt_deadline))
                raw.connect(address)
                raw.settimeout(remaining(attempt_deadline))
                self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
                self.sock.settimeout(remaining(self.deadline))
                return
            except OSError:
                if index == len(addresses) - 1:
                    raise
            finally:
                if raw is not None and self.sock is None:
                    raw.close()


def fetch(artifact: dict[str, Any], deadline: float, *, max_bytes: int = MAX_ARCHIVE) -> bytes:
    """Download one approved artifact without exposing redirects or error content."""
    artifact_policy(artifact)
    limit = min(MAX_ARCHIVE, max_bytes)
    require(limit > 0, "total-size")
    url = artifact["url"]
    chain = artifact.get("redirects", [])
    signed = False
    try:
        for hop in range(4):
            host, target = canonical_url(url, signed=signed)
            connection = PinnedHTTPS(host, deadline)
            try:
                connection.putrequest("GET", target, skip_accept_encoding=True)
                connection.putheader("User-Agent", "maf-dependency-preparation/1")
                connection.putheader("Accept", "application/octet-stream")
                connection.putheader("Accept-Encoding", "identity")
                connection.endheaders()
                transport = connection.sock
                assert transport is not None
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    locations = response.headers.get_all("Location", [])
                    require(len(locations) == 1 and hop < 3, "redirect-limit")
                    location = locations[0]
                    repository = artifact.get("github_repository_id")
                    if repository is not None and hop == 0:
                        redirected_host, redirected_target = canonical_url(location, signed=True)
                        require(
                            redirected_host == "release-assets.githubusercontent.com",
                            "redirect-host",
                        )
                        require(
                            re.fullmatch(
                                r"/github-production-release-asset/"
                                + repository
                                + r"/[a-zA-Z0-9-]+",
                                redirected_target.split("?", 1)[0],
                            ),
                            "redirect-artifact",
                        )
                        signed = True
                    else:
                        require(not signed and hop < len(chain), "redirect-unapproved")
                        canonical_url(location)
                        require(location == chain[hop], "redirect-unapproved")
                    url = location
                    continue
                require(response.status == 200, "response-status")
                require(
                    artifact.get("github_repository_id") is not None or hop == len(chain),
                    "redirect-unapproved",
                )
                require(
                    response.getheader("Content-Encoding", "identity") == "identity",
                    "response-encoding",
                )
                lengths = response.headers.get_all("Content-Length", [])
                require(len(lengths) <= 1, "response-length")
                expected = int(lengths[0]) if lengths else None
                require(expected is None or 0 <= expected <= limit, "download-size")
                data = bytearray()
                while True:
                    transport.settimeout(remaining(deadline))
                    chunk = response.read1(min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    require(len(data) <= limit, "download-size")
                require(expected is None or len(data) == expected, "download-incomplete")
                require(hashlib.sha256(data).hexdigest() == artifact["sha256"], "artifact-mismatch")
                return bytes(data)
            finally:
                connection.close()
    except Refused:
        raise
    except Exception:
        raise Refused("transfer-failed") from None
    raise Refused("redirect-limit")


def zip_files(data: bytes, *, limit: int, retain: bool = True) -> dict[str, bytes]:
    """Read regular archive entries as bounded data; never extract to the host filesystem.

    Without ``retain``, entries are checked in chunks and returned empty.
    """
    result: dict[str, bytes] = {}
    seen: set[str] = set()
    expanded = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(len(archive.infolist()) <= 4096, "archive-entries")
        for entry in archive.infolist():
            name = relative_path(entry.filename.removesuffix("/"))
            require(name.casefold() not in seen, "archive-collision")
            seen.add(name.casefold())
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(mode in {0, stat.S_IFREG, stat.S_IFDIR}, "archive-special")
            require(not entry.flag_bits & 1, "archive-encryption")
            if entry.is_dir():
                continue
            require(mode != stat.S_IFDIR, "archive-type")
            expanded += entry.file_size
            require(expanded <= limit, "archive-expansion")
            parts: list[bytes] = []
            size = 0
            with archive.open(entry) as stream:
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    require(size <= entry.file_size, "archive-size")
                    if retain:
                        parts.append(chunk)
            require(size == entry.file_size, "archive-size")
            result[name] = b"".join(parts)
    require(result, "archive-empty")
    return result


def module_files(module: dict[str, Any], data: bytes, engine: str) -> dict[str, str]:
    """Preserve an approved local module graph and its original source bytes."""
    archive = zip_files(data, limit=MAX_TEXT)
    prefix = module["subdir"] + "/"
    files = {
        name[len(prefix) :]: content for name, content in archive.items() if name.startswith(prefix)
    }
    require(files and len(files) <= 256, "module-files")
    texts: dict[str, str] = {}
    observed: dict[str, dict[str, str]] = {}
    families: dict[str, bool] = {}
    for name, data in files.items():
        parts = name.split("/")
        require(
            not any(part.startswith(".") for part in parts[:-1])
            and (parts[-1] == ".terraform.lock.hcl" or not parts[-1].startswith(".")),
            "module-hidden",
        )
        require(
            not name.casefold().endswith(
                (".tfstate", ".tfstate.backup", ".tfvars", ".tfvars.json")
            ),
            "module-state",
        )
        require(b"\x00" not in data, "module-text")
        try:
            texts[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("module-text") from None
        if not name.endswith((".tf", ".tf.json", ".tofu", ".tofu.json")):
            continue
        require(
            not name.endswith(
                ("override.tf", "override.tf.json", "override.tofu", "override.tofu.json")
            ),
            "module-override",
        )
        is_tofu = name.endswith((".tofu", ".tofu.json"))
        require(engine == "opentofu" or not is_tofu, "module-engine")
        extension = ".tofu" if is_tofu else ".tf"
        ending = ".json" if name.endswith(".json") else ""
        counterpart = (
            name.removesuffix(extension + ending) + (".tf" if is_tofu else ".tofu") + ending
        )
        require(counterpart not in files, "module-precedence")
        directory = posixpath.dirname(name) or "."
        require(families.setdefault(directory, is_tofu) == is_tofu, "module-precedence")
        edges = observed.setdefault(directory, {})
        parsed = (
            unique_json(texts[name], "module-json-duplicate")
            if name.endswith(".json")
            else hcl2.loads(texts[name])
        )
        blocks = parsed.get("module", {})
        if isinstance(blocks, list):
            pairs = [item for block in blocks for item in block.items()]
        else:
            pairs = list(blocks.items())
        for label, block in pairs:
            if not name.endswith(".json"):
                label = json.loads(label) if label.startswith('"') else label
            require(label not in edges and isinstance(block, dict), "module-duplicate")
            source = block.get("source")
            if not isinstance(source, str):
                raise Refused("module-remote")
            if not name.endswith(".json"):
                require(source.startswith('"'), "module-source")
                source = json.loads(source)
            require(isinstance(source, str) and source.startswith(("./", "../")), "module-remote")
            require(re.fullmatch(r"[A-Za-z0-9_./-]+", source), "module-source")
            target = posixpath.normpath(posixpath.join(directory, source))
            relative_path(target, dot=True)
            edges[label] = target
    require(observed == module["graph"], "module-graph-mismatch")
    visited: set[str] = set()

    def visit(directory: str, ancestors: set[str]) -> None:
        require(directory not in ancestors, "module-cycle")
        if directory in visited:
            return
        for target in observed[directory].values():
            visit(target, ancestors | {directory})
        visited.add(directory)

    visit(".", set())
    require(visited == observed.keys(), "module-unreachable")
    return texts


def satisfies(version: str, constraint: str, decision: str) -> bool:
    """Check a release version against the constraint syntax both registries share."""
    target = tuple(int(part) for part in version.split("."))
    for term in constraint.split(","):
        match = re.fullmatch(_CONSTRAINT, term)
        require(match, decision)
        assert match is not None
        operator, given = match.group(1) or "=", [int(part) for part in match.group(2).split(".")]
        bound = tuple(given + [0] * (3 - len(given)))
        # A one-segment pessimistic bound differs between the module and provider libraries.
        require(operator != "~>" or len(given) > 1, decision)
        allowed = {
            "=": target == bound,
            "!=": target != bound,
            ">": target > bound,
            ">=": target >= bound,
            "<": target < bound,
            "<=": target <= bound,
            "~>": target >= bound and target[: len(given) - 1] == bound[: len(given) - 1],
        }[operator]
        if not allowed:
            return False
    return True


def _literal(value: Any, json_syntax: bool) -> str | None:
    """Return a string attribute only when it is a literal without template sequences."""
    if not json_syntax:
        if not (isinstance(value, str) and value.startswith('"')):
            return None
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, str) or "${" in value or "%{" in value:
        return None
    return value


def _bodies(parsed: Any, kind: str) -> list[dict[str, Any]]:
    """Return every body of one block type in either configuration syntax."""
    value = parsed.get(kind, []) if isinstance(parsed, dict) else []
    bodies = value if isinstance(value, list) else [value]
    require(all(isinstance(body, dict) for body in bodies), "module-source")
    return bodies


def _blocks(parsed: Any, kind: str, json_syntax: bool) -> list[tuple[str, Any]]:
    """Flatten one labelled block type, unquoting native-syntax labels."""
    return [
        (json.loads(label) if not json_syntax and label.startswith('"') else label, body)
        for block in _bodies(parsed, kind)
        for label, body in block.items()
        if label != "__is_block__"
    ]


def _provider_source(source: str) -> str | None:
    """Normalize a required provider address; built-in providers need no artifact."""
    parts = source.lower().split("/")
    require(1 <= len(parts) <= 3 and all(parts), "registry-provider")
    parts = ([REGISTRY_HOST, "hashicorp"] if len(parts) == 1 else [REGISTRY_HOST]) + parts
    parts = parts[-3:]
    return None if parts[:2] == ["terraform.io", "builtin"] else "/".join(parts)


def registry_module_files(
    module: dict[str, Any], data: bytes, catalog: dict[str, dict[str, Any]], providers: list[Any]
) -> tuple[dict[str, bytes], dict[str, dict[str, str]]]:
    """Select a registry package's module directories and prove its edges and providers.

    Returns the selected file bytes and the source string Terraform records for each edge.
    """
    archive = zip_files(data, limit=MAX_TEXT)
    repository = module["artifact"]["url"].split("/")[4]
    prefix = f"{repository}-{module['revision']}/"
    graph = module["graph"]
    files = {
        name[len(prefix) :]: content
        for name, content in archive.items()
        if name.startswith(prefix)
        and (posixpath.dirname(name[len(prefix) :]) or ".") in graph
        and not posixpath.basename(name).startswith(".")
    }
    require(len(files) <= 256, "module-files")
    pins = {item["source"]: item["version"] for item in providers}
    observed: dict[str, dict[str, Any]] = {directory: {} for directory in graph}
    sources: dict[str, dict[str, str]] = {directory: {} for directory in graph}
    declared: dict[str, set[str]] = {directory: set() for directory in graph}
    implied: dict[str, set[str]] = {directory: set() for directory in graph}
    for name, content in sorted(files.items()):
        require(
            not name.casefold().endswith(
                (".tfstate", ".tfstate.backup", ".tfvars", ".tfvars.json")
            ),
            "module-state",
        )
        require(b"\x00" not in content, "module-text")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("module-text") from None
        if not name.endswith((".tf", ".tf.json", ".tofu", ".tofu.json")):
            continue
        require(not name.endswith((".tofu", ".tofu.json")), "module-engine")
        stem = posixpath.basename(name).removesuffix(".json").removesuffix(".tf")
        require(stem != "override" and not stem.endswith("_override"), "module-override")
        json_syntax = name.endswith(".json")
        parsed = unique_json(text, "module-json-duplicate") if json_syntax else hcl2.loads(text)
        directory = posixpath.dirname(name) or "."
        edges = observed[directory]
        for label, block in _blocks(parsed, "module", json_syntax):
            require(label not in edges and isinstance(block, dict), "module-duplicate")
            source = _literal(block.get("source"), json_syntax)
            require(source is not None, "module-source")
            assert source is not None
            if source.startswith(("./", "../")):
                require(re.fullmatch(r"[A-Za-z0-9_./-]+", source), "module-source")
                target = posixpath.normpath(posixpath.join(directory, source))
                relative_path(target, dot=True)
                edges[label] = {"local": target}
                clean = posixpath.normpath(source)
                sources[directory][label] = clean if clean.startswith("../") else "./" + clean
                continue
            match = re.fullmatch(rf"(?:([^/]+)/)?({_REGISTRY_PACKAGE})", source)
            require(match, "module-remote")
            assert match is not None
            require((match.group(1) or REGISTRY_HOST).lower() == REGISTRY_HOST, "registry-host")
            address = f"{REGISTRY_HOST}/{match.group(2)}"
            constraint = _literal(block.get("version"), json_syntax)
            require(constraint is not None, "registry-version")
            assert constraint is not None
            target = next(
                (
                    item
                    for item in catalog.values()
                    if item["source"].casefold() == address.casefold()
                ),
                None,
            )
            require(target is not None, "registry-edge")
            assert target is not None
            require(
                satisfies(target["version"], constraint, "registry-constraint"),
                "registry-constraint",
            )
            edges[label] = {"registry": target["name"]}
            sources[directory][label] = address
        for settings in _bodies(parsed, "terraform"):
            for local, requirement in _blocks(settings, "required_providers", json_syntax):
                source: str | None = local
                constraint = requirement
                if isinstance(requirement, dict):
                    if "source" in requirement:
                        source = _literal(requirement["source"], json_syntax)
                    constraint = requirement.get("version")
                require(source is not None, "registry-provider")
                assert source is not None
                declared[directory].add(local)
                address = _provider_source(source)
                if address is None:
                    continue
                require(address in pins, "registry-provider")
                if constraint is not None:
                    constraint = _literal(constraint, json_syntax)
                    require(
                        constraint is not None
                        and re.fullmatch(_RELEASE, pins[address])
                        and satisfies(pins[address], constraint, "registry-provider"),
                        "registry-provider",
                    )
        for kind in ("resource", "data", "ephemeral"):
            implied[directory].update(
                label.split("_")[0] for label, _ in _blocks(parsed, kind, json_syntax)
            )
    for directory in graph:
        for local in implied[directory] - declared[directory] - {"terraform"}:
            require(f"{REGISTRY_HOST}/hashicorp/{local}" in pins, "registry-provider")
    require(
        all(
            any(
                (posixpath.dirname(name) or ".") == directory
                for name in files
                if name.endswith((".tf", ".tf.json"))
            )
            for directory in graph
        ),
        "registry-directory",
    )
    require(observed == graph, "module-graph-mismatch")
    visited: set[str] = set()

    def visit(directory: str, ancestors: set[str]) -> None:
        require(directory not in ancestors, "module-cycle")
        if directory in visited:
            return
        for edge in graph[directory].values():
            if "local" in edge:
                visit(edge["local"], ancestors | {directory})
        visited.add(directory)

    visit(".", set())
    require(visited == graph.keys(), "module-unreachable")
    return files, sources


def registry_inventory(
    name: str, catalog: dict[str, dict[str, Any]], sources: dict[str, dict[str, dict[str, str]]]
) -> list[dict[str, str]]:
    """Expand the manifest records Terraform would write below one call of this package."""
    records: list[dict[str, str]] = []

    def expand(package: str, directory: str, prefix: str) -> None:
        for label, edge in sorted(catalog[package]["graph"][directory].items()):
            key = f"{prefix}.{label}" if prefix else label
            record = {"key": key, "source": sources[package][directory][label]}
            if "local" in edge:
                target, child = package, edge["local"]
            else:
                target, child = edge["registry"], "."
                record["version"] = catalog[target]["version"]
            records.append({**record, "package": target, "dir": child})
            require(len(records) <= MAX_INVENTORY, "registry-inventory")
            expand(target, child, key)

    expand(name, ".", "")
    return records


def policy_contract() -> dict[str, Any]:
    """Fingerprint the implementation and effective limits independently of artifact approvals."""
    source = Path(__file__).read_text(encoding="utf-8").encode("utf-8")
    return {
        "schema": 1,
        "implementation_sha256": hashlib.sha256(source).hexdigest(),
        "limits": {
            "manifest": MAX_MANIFEST,
            "archive": MAX_ARCHIVE,
            "total": MAX_TOTAL,
            "text": MAX_TEXT,
            "provider_expanded": MAX_PROVIDER_EXPANDED,
            "response_head": MAX_RESPONSE_HEAD,
            "inventory": MAX_INVENTORY,
            "deadline": DEADLINE,
        },
    }


def prepare(manifest: dict[str, Any], output: Path) -> str:
    """Write a fresh preparation directory; the CLI publishes it only on complete success."""
    checked_manifest(manifest)
    deadline = time.monotonic() + DEADLINE
    contract = policy_contract()
    manifest_digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = hashlib.sha256(
        json.dumps(
            {"manifest_sha256": manifest_digest, "contract": contract},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    output.mkdir()
    (output / "mirror").mkdir()
    (output / "modules").mkdir()
    (output / "registry").mkdir()
    receipt: dict[str, Any] = {
        "schema": 1,
        "engine": manifest["engine"],
        "policy_sha256": identity,
        "manifest_sha256": manifest_digest,
        "policy_contract": contract,
        "providers": [],
        "modules": [],
        "registry_modules": [],
    }
    catalog = {item["name"]: item for item in manifest.get("registry_modules", [])}
    sources: dict[str, dict[str, dict[str, str]]] = {}
    total = 0
    for kind in ("providers", "modules", "registry_modules"):
        for item in manifest.get(kind, []):
            data = fetch(item["artifact"], deadline, max_bytes=MAX_TOTAL - total)
            total += len(data)
            require(total <= MAX_TOTAL, "total-size")
            if kind == "providers":
                # Validate container structure before giving any archive to a guest installer.
                zip_files(data, limit=MAX_PROVIDER_EXPANDED, retain=False)
                provider_type = item["source"].split("/")[-1]
                name = (
                    f"terraform-provider-{provider_type}_{item['version']}_{item['platform']}.zip"
                )
                target = output / "mirror" / item["source"] / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                record = {key: item[key] for key in ("source", "version", "platform")}
            elif kind == "registry_modules":
                selected, sources[item["name"]] = registry_module_files(
                    item, data, catalog, manifest["providers"]
                )
                for name, content in selected.items():
                    target = output / "registry" / item["name"] / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                record = {
                    key: item[key] for key in ("name", "source", "version", "revision", "graph")
                }
                record["files"] = {
                    name: hashlib.sha256(content).hexdigest() for name, content in selected.items()
                }
            else:
                files = module_files(item, data, manifest["engine"])
                for name, text in files.items():
                    target = output / "modules" / item["name"] / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(text.encode("utf-8"))
                record = {key: item[key] for key in ("name", "revision", "graph")}
                record["files"] = {
                    name: hashlib.sha256(text.encode()).hexdigest() for name, text in files.items()
                }
            record["sha256"] = item["artifact"]["sha256"]
            record["provenance"] = item["artifact"]["provenance"]
            record["decision"] = "verified"
            receipt[kind].append(record)
    for record in receipt["registry_modules"]:
        record["inventory"] = registry_inventory(record["name"], catalog, sources)
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return identity


def load_manifest(data: bytes) -> dict[str, Any]:
    """Refuse duplicate JSON keys and oversized policy documents."""
    require(len(data) <= MAX_MANIFEST, "manifest-size")

    return checked_manifest(unique_json(data, "manifest-duplicate"))


def _worker(output: str) -> None:
    """Run only as the child of the CLI's deadline and temporary-output supervisor."""
    try:
        manifest = load_manifest(sys.stdin.buffer.read(MAX_MANIFEST + 1))
        prepare(manifest, Path(output))
    except Exception as exc:
        decision = str(exc) if isinstance(exc, Refused) else "preparation-failed"
        code = 64 + _WORKER_DECISIONS.index(decision) if decision in _WORKER_DECISIONS else 1
        raise SystemExit(code) from None


def main() -> None:
    """Isolate blocking DNS/HTTP/archive work behind a parent-enforced deadline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        require(args.manifest is not None, "manifest-required")
        with args.manifest.open("rb") as stream:
            data = stream.read(MAX_MANIFEST + 1)
        load_manifest(data)
        output = args.output.absolute()
        require(not output.exists() and not output.is_symlink(), "output-exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".terraform-preparation-", dir=output.parent
        ) as temporary:
            prepared = Path(temporary) / "prepared"
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import runpy, sys; runpy.run_path(sys.argv[1])['_worker'](sys.argv[2])",
                    str(Path(__file__).resolve()),
                    str(prepared),
                ],
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=DEADLINE,
                check=False,
            )
            if result.returncode != 0:
                index = result.returncode - 64
                decision = (
                    _WORKER_DECISIONS[index]
                    if 0 <= index < len(_WORKER_DECISIONS)
                    else "preparation-failed"
                )
                raise Refused(decision)
            # The parent directory and manifest are controlled by the operator, not a guest.
            require(not output.exists() and not output.is_symlink(), "output-exists")
            os.rename(prepared, output)
        print("Dependencies verified; receipt.json records artifact and policy identities.")
    except Exception as exc:
        decision = str(exc) if isinstance(exc, Refused) else "preparation-failed"
        print(f"Dependency preparation refused: {decision}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
