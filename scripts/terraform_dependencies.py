"""Prepare host-approved ZIP dependencies; run via the CLI for a hard wall-time bound."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.client
import importlib.util
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

# The launcher's reader decides the offline records at run time, so preparation reads with it.
_READER = Path(__file__).resolve().parents[1] / "images/terraform-sandbox/runner.py"
_SPEC = importlib.util.spec_from_file_location("terraform_runner", _READER)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)

MAX_MANIFEST = 4 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024
MAX_PROVIDER_EXPANDED = 1024 * 1024 * 1024
MAX_RESPONSE_HEAD = 32 * 1024
MAX_INVENTORY = 256
MAX_PROVIDERS = 64
MAX_REGISTRY_MODULES = 512
DEADLINE = 1800
REGISTRY_HOST = "registry.terraform.io"
_NAME = r"[a-z0-9][a-z0-9_-]{0,63}"
_PACKAGE = r"[a-z0-9][a-z0-9._-]{0,127}"
_LABEL = r"[A-Za-z_][A-Za-z0-9_-]{0,63}"
_REGISTRY_PART = r"[0-9A-Za-z](?:[0-9A-Za-z_-]{0,62}[0-9A-Za-z])?"
_REGISTRY_PACKAGE = rf"{_REGISTRY_PART}/{_REGISTRY_PART}/[0-9a-z]{{1,64}}"
_RELEASE = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
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
    "module-hidden module-json-duplicate module-name module-override module-parse "
    "module-precedence module-remote module-revision module-source module-state module-text "
    "module-unreachable modules output-exists provenance provider-conflict "
    "provider-platform provider-source provider-version providers redirect-artifact "
    "redirect-host redirect-limit redirect-unapproved registry-conflict registry-constraint "
    "registry-directory registry-edge registry-engine registry-excluded registry-host "
    "registry-inventory "
    "registry-modules registry-provider registry-revision registry-source registry-version "
    "response-encoding response-headers "
    "response-length response-status signed-content signed-fields signed-host signed-query "
    "total-size transfer-failed url-ascii url-authority url-fragment url-path url-query "
    "url-segments url-shape "
).split()


class Refused(ValueError):
    """A bounded policy decision safe to include in diagnostics.

    `status` is the HTTP status of the response being processed when the refusal was
    raised, or 0 where none had arrived. `fetch` sets it; the parser bounds it to three
    digits, and it is the only number an upstream server contributes to a refusal.
    """

    def __init__(self, decision: str, status: int = 0) -> None:
        super().__init__(decision)
        self.status = status


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


def native_items(text: str, reader: Any) -> Any:
    """Read native syntax with the launcher's parser; what it cannot follow is refused."""
    try:
        return reader(text)
    except (ValueError, RecursionError):
        raise Refused("module-parse") from None


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
    """Pin each registry package to a GitHub commit archive and a declared, acyclic graph.

    One source may be baked at several versions; each version is its own named package.
    """
    require(isinstance(modules, list) and len(modules) <= MAX_REGISTRY_MODULES, "registry-modules")
    require(not modules or engine == "terraform", "registry-engine")
    targets: dict[str, set[str]] = {}
    identities: set[tuple[str, str]] = set()
    entries: list[tuple[str, str]] = []
    graphs: dict[str, dict[str, Any]] = {}
    for module in modules:
        exact_keys(module, {"name", "source", "version", "revision", "graph", "artifact"})
        name, source, version = module["name"], module["source"], module["version"]
        require(isinstance(name, str) and re.fullmatch(_PACKAGE, name), "module-name")
        relative_path(name)
        require(isinstance(source, str), "registry-source")
        require(
            re.fullmatch(re.escape(REGISTRY_HOST) + "/" + _REGISTRY_PACKAGE, source),
            "registry-source",
        )
        require(isinstance(version, str) and re.fullmatch(_RELEASE, version), "registry-version")
        identity = (source.casefold(), version)
        require(name not in targets and identity not in identities, "registry-conflict")
        identities.add(identity)
        revision = module["revision"]
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
        require(isinstance(graph, dict) and 0 < len(graph) <= 64, "module-graph")
        graphs[name] = graph
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
                require(
                    isinstance(edge, dict) and ("local" in edge or "registry" in edge),
                    "registry-edge",
                )
                if "local" in edge:
                    require(len(edge) == 1, "registry-edge")
                    require(edge["local"] in graph, "module-edge-target")
                else:
                    exact_keys(edge, {"registry"}, {"dir"})
                    require(isinstance(edge["registry"], str), "registry-edge")
                    if "dir" in edge:
                        require(edge["dir"] != ".", "registry-edge")
                        relative_path(edge["dir"])
                    targets[name].add(edge["registry"])
                    entries.append((edge["registry"], edge.get("dir", ".")))
    for target, directory in entries:
        require(target in graphs and directory in graphs[target], "registry-edge")
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


def checked_exclusions(excluded: Any) -> None:
    """The generator's record of roots it could not bake; preparation only checks its shape."""
    require(isinstance(excluded, list) and len(excluded) <= 1024, "registry-excluded")
    for item in excluded:
        exact_keys(item, {"source", "version", "reason"})
        require(
            isinstance(item["source"], str)
            and re.fullmatch(
                re.escape(REGISTRY_HOST) + r"(?:/[0-9A-Za-z_-]{1,128}){3}", item["source"]
            )
            and isinstance(item["version"], str)
            and re.fullmatch(_RELEASE, item["version"])
            and isinstance(item["reason"], str)
            and re.fullmatch(r"[\x20-\x7e]{1,300}", item["reason"]),
            "registry-excluded",
        )


def checked_manifest(value: Any) -> dict[str, Any]:
    """Bind provider identities and module graphs to a single engine policy."""
    exact_keys(
        value, {"schema", "engine", "providers", "modules"}, {"registry_modules", "excluded"}
    )
    require(value["schema"] == 1 and type(value["schema"]) is int, "manifest-schema")
    require(value["engine"] in {"terraform", "opentofu"}, "engine")
    require(
        isinstance(value["providers"], list) and len(value["providers"]) <= MAX_PROVIDERS,
        "providers",
    )
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
    checked_exclusions(value.get("excluded", []))
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
        # Set the floor here rather than trust the interpreter's or OpenSSL's default.
        self.tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
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
                try:
                    if response.status in {301, 302, 303, 307, 308}:
                        locations = response.headers.get_all("Location", [])
                        require(len(locations) == 1 and hop < 3, "redirect-limit")
                        location = locations[0]
                        repository = artifact.get("github_repository_id")
                        if repository is not None and hop == 0:
                            redirected_host, redirected_target = canonical_url(
                                location, signed=True
                            )
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
                    require(
                        hashlib.sha256(data).hexdigest() == artifact["sha256"], "artifact-mismatch"
                    )
                    return bytes(data)
                except Refused as refusal:
                    # Everything above runs with the response in hand, refusal or not.
                    refusal.status = response.status
                    raise
                except Exception:
                    raise Refused("transfer-failed", response.status) from None
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


def package_hash(data: bytes) -> str:
    """Terraform's h1 package hash, computed from provider ZIP bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        lines = "".join(
            f"{hashlib.sha256(archive.read(name)).hexdigest()}  {name}\n"
            for name in sorted(item.filename for item in archive.infolist() if not item.is_dir())
        )
    return "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()


def zip_entry_digests(data: bytes) -> dict[str, str]:
    """SHA-256 of each regular ZIP entry, keyed by entry name."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in sorted(item.filename for item in archive.infolist() if not item.is_dir())
        }


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
        if name.endswith(".json"):
            blocks = unique_json(texts[name], "module-json-duplicate").get("module", {})
            pairs = (
                [item for block in blocks for item in block.items()]
                if isinstance(blocks, list)
                else list(blocks.items())
            )
        else:
            pairs = list(native_items(texts[name], runner._hcl_module_calls).items())
        for label, block in pairs:
            require(label not in edges and isinstance(block, dict), "module-duplicate")
            require("source" in block, "module-remote")
            source = block["source"]
            require(source is not None, "module-source")
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
    """Check a release against a registry constraint with the launcher's rules."""
    try:
        return runner.satisfies(version, constraint)
    except ValueError:
        raise Refused(decision) from None


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
    """Flatten one labelled block type, unquoting native-syntax labels and skipping comments."""
    skipped = {"//"} if json_syntax else {"__is_block__", "__comments__", "__inline_comments__"}
    return [
        (json.loads(label) if not json_syntax and label.startswith('"') else label, body)
        for block in _bodies(parsed, kind)
        for label, body in block.items()
        if label not in skipped
    ]


_ABSENT = object()


def _provider_source(source: str) -> str | None:
    """Normalize a required provider address; built-in providers need no artifact."""
    parts = source.lower().split("/")
    require(1 <= len(parts) <= 3 and all(parts), "registry-provider")
    parts = ([REGISTRY_HOST, "hashicorp"] if len(parts) == 1 else [REGISTRY_HOST]) + parts
    parts = parts[-3:]
    return None if parts[:2] == ["terraform.io", "builtin"] else "/".join(parts)


def registry_archive_files(module: dict[str, Any], data: bytes) -> dict[str, bytes]:
    """Read only what a package bakes: files in its graph directories, not hidden, not .tofu.

    Terraform ignores .tofu files. Entries outside the selection are never read.
    """
    repository = module["artifact"]["url"].split("/")[4]
    prefix = f"{repository}-{module['revision']}/"
    graph = module["graph"]
    files: dict[str, bytes] = {}
    expanded = 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        require(len(archive.infolist()) <= 65536, "archive-entries")
        for entry in archive.infolist():
            name = entry.filename.removeprefix(prefix)
            if (
                name == entry.filename
                or entry.is_dir()
                or (posixpath.dirname(name) or ".") not in graph
                or posixpath.basename(name).startswith(".")
                or name.endswith((".tofu", ".tofu.json"))
            ):
                continue
            relative_path(name)
            require(name.casefold() not in {item.casefold() for item in files}, "archive-collision")
            mode = stat.S_IFMT(entry.external_attr >> 16)
            require(mode in {0, stat.S_IFREG, stat.S_IFDIR}, "archive-special")
            require(mode != stat.S_IFDIR, "archive-type")
            require(not entry.flag_bits & 1, "archive-encryption")
            expanded += entry.file_size
            require(expanded <= MAX_TEXT, "archive-expansion")
            with archive.open(entry) as stream:
                content = stream.read(entry.file_size + 1)
            require(len(content) == entry.file_size, "archive-size")
            files[name] = content
    require(len(files) <= 256, "module-files")
    return files


def configuration(name: str, text: str) -> tuple[dict[str, Any], list[tuple[str, Any]], set[str]]:
    """Read one file's module calls, provider requirements and implied provider names."""
    if not name.endswith(".json"):
        calls = native_items(text, runner._hcl_module_calls)
        requirements: list[tuple[str, Any]] = []
        types: set[str] = set()
        for kind, labels, body in native_items(text, runner.parse_hcl):
            if kind in ("resource", "data", "ephemeral") and labels and labels[0]:
                types.add(labels[0].split("_")[0])
            if kind == "terraform" and labels == []:
                for inner, inner_labels, entries in body:
                    if inner == "required_providers" and inner_labels == []:
                        requirements += [
                            (local, value) for local, flag, value in entries if flag is None
                        ]
        return calls, requirements, types
    parsed = unique_json(text, "module-json-duplicate")
    calls = {}
    for label, block in _blocks(parsed, "module", True):
        calls[label] = (
            None
            if label in calls or not isinstance(block, dict)
            else {key: _literal(block[key], True) for key in ("source", "version") if key in block}
        )
    requirements = [
        (
            local,
            {key: _literal(value, True) for key, value in requirement.items()}
            if isinstance(requirement, dict)
            else _literal(requirement, True),
        )
        for settings in _bodies(parsed, "terraform")
        for local, requirement in _blocks(settings, "required_providers", True)
    ]
    types = {
        label.split("_")[0]
        for kind in ("resource", "data", "ephemeral")
        for label, _ in _blocks(parsed, kind, True)
    }
    return calls, requirements, types


def directory_configuration(
    texts: list[tuple[str, str]],
) -> tuple[dict[str, Any], dict[str, list[Any]], set[str]]:
    """Merge one directory's files in Terraform's order: primary files, then override files.

    Returns module calls, provider requirements by local name, and implied provider names.
    """
    found: list[tuple[str, Any]] = []
    required: dict[str, list[Any]] = {}
    implied: set[str] = set()
    for name, text in sorted(texts, key=lambda item: (runner.is_override(item[0]), item[0])):
        calls, requirements, types = configuration(name, text)
        found.append((name, calls))
        implied |= types
        for local, requirement in requirements:
            if runner.is_override(name):
                required[local] = [requirement]
            else:
                required.setdefault(local, []).append(requirement)
    return runner.merge_module_calls(found), required, implied


def provider_needs(required: dict[str, list[Any]], implied: set[str]) -> dict[str, list[str]]:
    """Map each registry provider address to its literal constraints; builtins need nothing."""
    needs: dict[str, list[str]] = {}
    for local, requirements in required.items():
        for requirement in requirements:
            source, constraint = local, requirement
            if isinstance(requirement, dict):
                source = requirement.get("source", local)
                constraint = requirement.get("version", _ABSENT)
            if not isinstance(source, str) or not (
                constraint is _ABSENT or isinstance(constraint, str)
            ):
                raise Refused("registry-provider")
            address = _provider_source(source)
            if address is not None:
                needs.setdefault(address, [])
                if isinstance(constraint, str):
                    needs[address].append(constraint)
    for local in implied - required.keys() - {"terraform"}:
        needs.setdefault(f"{REGISTRY_HOST}/hashicorp/{local}", [])
    return needs


def registry_module_files(
    module: dict[str, Any], data: bytes, catalog: dict[str, dict[str, Any]], providers: list[Any]
) -> tuple[dict[str, bytes], dict[str, dict[str, str]]]:
    """Select a registry package's module directories and prove its edges and providers.

    Returns the selected file bytes and the source string Terraform records for each edge.
    """
    files = registry_archive_files(module, data)
    graph = module["graph"]
    pins: dict[str, list[str]] = {}
    for item in providers:
        pins.setdefault(item["source"], []).append(item["version"])
    texts: dict[str, list[tuple[str, str]]] = {directory: [] for directory in graph}
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
        if name.endswith((".tf", ".tf.json")):
            texts[posixpath.dirname(name) or "."].append((posixpath.basename(name), text))
    read = {directory: directory_configuration(texts[directory]) for directory in graph}
    observed: dict[str, dict[str, Any]] = {directory: {} for directory in graph}
    sources: dict[str, dict[str, str]] = {directory: {} for directory in graph}
    for directory in graph:
        calls, required, implied = read[directory]
        for label, arguments in sorted(calls.items()):
            require(arguments is not None, "module-duplicate")
            source = arguments.get("source")
            require(source is not None, "module-source")
            if source.startswith(("./", "../")):
                require(re.fullmatch(r"[A-Za-z0-9_./-]+", source), "module-source")
                target = posixpath.normpath(posixpath.join(directory, source))
                relative_path(target, dot=True)
                observed[directory][label] = {"local": target}
                clean = posixpath.normpath(source)
                sources[directory][label] = clean if clean.startswith("../") else "./" + clean
                continue
            match = runner._REGISTRY_SOURCE.fullmatch(source)
            require(match, "module-remote")
            assert match is not None
            require((match.group(1) or REGISTRY_HOST).lower() == REGISTRY_HOST, "registry-host")
            subdir = match.group(3)
            if subdir is not None:
                relative_path(subdir)
            address = f"{REGISTRY_HOST}/{match.group(2)}"
            constraint = arguments.get("version")
            if constraint is None:
                raise Refused("registry-version")
            edge = graph[directory].get(label)
            target = catalog.get(edge.get("registry", "")) if isinstance(edge, dict) else None
            if (
                target is None
                or target["source"].casefold() != address.casefold()
                or edge.get("dir") != subdir
            ):
                raise Refused("registry-edge")
            require(
                satisfies(target["version"], constraint, "registry-constraint"),
                "registry-constraint",
            )
            observed[directory][label] = edge
            sources[directory][label] = address + (f"//{subdir}" if subdir else "")
        for address, constraints in provider_needs(required, implied).items():
            require(
                any(
                    re.fullmatch(_RELEASE, version)
                    and all(satisfies(version, item, "registry-provider") for item in constraints)
                    for version in pins.get(address, [])
                )
                or (address in pins and not constraints),
                "registry-provider",
            )
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
    entries = {"."} & graph.keys()
    for other in catalog.values():
        for edges in other["graph"].values():
            entries.update(
                edge.get("dir", ".")
                for edge in edges.values()
                if edge.get("registry") == module["name"]
            )
    visited: set[str] = set()

    def visit(directory: str, ancestors: set[str]) -> None:
        require(directory not in ancestors, "module-cycle")
        if directory in visited:
            return
        for edge in graph[directory].values():
            if "local" in edge:
                visit(edge["local"], ancestors | {directory})
        visited.add(directory)

    for entry in sorted(entries):
        visit(entry, set())
    require(visited == graph.keys(), "module-unreachable")
    return files, sources


def registry_inventory(
    name: str,
    directory: str,
    catalog: dict[str, dict[str, Any]],
    sources: dict[str, dict[str, dict[str, str]]],
) -> list[dict[str, str]]:
    """Expand the manifest records Terraform would write below one call of this directory."""
    records: list[dict[str, str]] = []

    def expand(package: str, directory: str, prefix: str) -> None:
        for label, edge in sorted(catalog[package]["graph"][directory].items()):
            key = f"{prefix}.{label}" if prefix else label
            record = {"key": key, "source": sources[package][directory][label]}
            if "local" in edge:
                target, child = package, edge["local"]
            else:
                target, child = edge["registry"], edge.get("dir", ".")
                record["version"] = catalog[target]["version"]
            records.append({**record, "package": target, "dir": child})
            require(len(records) <= MAX_INVENTORY, "registry-inventory")
            expand(target, child, key)

    expand(name, directory, "")
    return records


def policy_contract() -> dict[str, Any]:
    """Fingerprint the implementation and effective limits independently of artifact approvals."""
    source = Path(__file__).read_text(encoding="utf-8").encode("utf-8")
    reader = _READER.read_text(encoding="utf-8").encode("utf-8")
    return {
        "schema": 1,
        "implementation_sha256": hashlib.sha256(source).hexdigest(),
        "reader_sha256": hashlib.sha256(reader).hexdigest(),
        "limits": {
            "manifest": MAX_MANIFEST,
            "archive": MAX_ARCHIVE,
            "total": MAX_TOTAL,
            "text": MAX_TEXT,
            "provider_expanded": MAX_PROVIDER_EXPANDED,
            "response_head": MAX_RESPONSE_HEAD,
            "inventory": MAX_INVENTORY,
            "providers": MAX_PROVIDERS,
            "registry_modules": MAX_REGISTRY_MODULES,
            "deadline": DEADLINE,
        },
    }


def artifact_sequence(manifest: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The order preparation downloads in, so one position names one artifact."""
    return [
        (kind, item)
        for kind in ("providers", "modules", "registry_modules")
        for item in manifest.get(kind, [])
    ]


def prepare(manifest: dict[str, Any], output: Path, *, progress: Path | None = None) -> str:
    """Write a fresh preparation directory; the CLI publishes it only on complete success.

    `progress` receives the position in `artifact_sequence` of the artifact being fetched,
    which is how the CLI names what a refusal — or a crash, or the deadline — stopped on.
    """
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
    for position, (kind, item) in enumerate(artifact_sequence(manifest)):
        if progress is not None:
            progress.write_text(f"artifact {position}\n", encoding="ascii", newline="\n")
        data = fetch(item["artifact"], deadline, max_bytes=MAX_TOTAL - total)
        total += len(data)
        require(total <= MAX_TOTAL, "total-size")
        if kind == "providers":
            # Validate container structure before giving any archive to a guest installer.
            zip_files(data, limit=MAX_PROVIDER_EXPANDED, retain=False)
            provider_type = item["source"].split("/")[-1]
            name = f"terraform-provider-{provider_type}_{item['version']}_{item['platform']}.zip"
            target = output / "mirror" / item["source"] / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            record = {key: item[key] for key in ("source", "version", "platform")}
            record["files"] = zip_entry_digests(data)
            record["h1"] = package_hash(data)
        elif kind == "registry_modules":
            selected, sources[item["name"]] = registry_module_files(
                item, data, catalog, manifest["providers"]
            )
            for name, content in selected.items():
                target = output / "registry" / item["name"] / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            record = {key: item[key] for key in ("name", "source", "version", "revision", "graph")}
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
        record["inventories"] = {
            directory: registry_inventory(record["name"], directory, catalog, sources)
            for directory in sorted(record["graph"])
        }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return identity


def load_manifest(data: bytes) -> dict[str, Any]:
    """Refuse duplicate JSON keys and oversized policy documents."""
    require(len(data) <= MAX_MANIFEST, "manifest-size")

    return checked_manifest(unique_json(data, "manifest-duplicate"))


def _worker(output: str, progress: str) -> None:
    """Run only as the child of the CLI's deadline and temporary-output supervisor."""
    try:
        manifest = load_manifest(sys.stdin.buffer.read(MAX_MANIFEST + 1))
        prepare(manifest, Path(output), progress=Path(progress))
    except Exception as exc:
        decision = str(exc) if isinstance(exc, Refused) else "preparation-failed"
        status = exc.status if isinstance(exc, Refused) else 0
        if status:
            # A failed note costs the status alone; the exit code still carries the decision.
            with contextlib.suppress(OSError):
                with open(progress, "a", encoding="ascii", newline="\n") as note:
                    note.write(f"status {status}\n")
        code = 64 + _WORKER_DECISIONS.index(decision) if decision in _WORKER_DECISIONS else 1
        raise SystemExit(code) from None


def artifact_name(kind: str, item: dict[str, Any]) -> str:
    """Identify one manifest entry the way the policy that approved it names it."""
    if kind == "providers":
        return f"provider {item['source']} {item['version']} {item['platform']}"
    if kind == "registry_modules":
        return f"registry module {item['source']} {item['version']}"
    return f"module {item['name']}"


def refusal_context(progress: Path, manifest: dict[str, Any]) -> str:
    """Name what preparation stopped on, reading every word from the operator's manifest.

    A position and an HTTP status are all the worker contributes, so no byte an upstream
    server sent can reach the message. An unreadable or unrecognised note names nothing.
    """
    try:
        note = progress.read_text(encoding="ascii")
    except (OSError, ValueError):
        return ""
    match = re.fullmatch(r"artifact ([0-9]{1,4})\n(?:status ([0-9]{1,3})\n)?", note)
    sequence = artifact_sequence(manifest)
    if match is None or int(match[1]) >= len(sequence):
        return ""
    kind, item = sequence[int(match[1])]
    lines = [f"  artifact: {artifact_name(kind, item)}", f"  url: {item['artifact']['url']}"]
    if match[2] is not None:
        lines.append(f"  status: {int(match[2])}")
    return "\n" + "\n".join(lines)


def main() -> None:
    """Isolate blocking DNS/HTTP/archive work behind a parent-enforced deadline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    context = ""
    try:
        require(args.manifest is not None, "manifest-required")
        with args.manifest.open("rb") as stream:
            data = stream.read(MAX_MANIFEST + 1)
        manifest = load_manifest(data)
        output = args.output.absolute()
        require(not output.exists() and not output.is_symlink(), "output-exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".terraform-preparation-", dir=output.parent
        ) as temporary:
            prepared = Path(temporary) / "prepared"
            progress = Path(temporary) / "progress"
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import runpy, sys; runpy.run_path(sys.argv[1])['_worker'](*sys.argv[2:])",
                        str(Path(__file__).resolve()),
                        str(prepared),
                        str(progress),
                    ],
                    input=data,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=DEADLINE,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                context = refusal_context(progress, manifest)
                raise
            if result.returncode != 0:
                index = result.returncode - 64
                decision = (
                    _WORKER_DECISIONS[index]
                    if 0 <= index < len(_WORKER_DECISIONS)
                    else "preparation-failed"
                )
                context = refusal_context(progress, manifest)
                raise Refused(decision)
            # The parent directory and manifest are controlled by the operator, not a guest.
            require(not output.exists() and not output.is_symlink(), "output-exists")
            os.rename(prepared, output)
        print("Dependencies verified; receipt.json records artifact and policy identities.")
    except Exception as exc:
        decision = str(exc) if isinstance(exc, Refused) else "preparation-failed"
        print(f"Dependency preparation refused: {decision}{context}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
