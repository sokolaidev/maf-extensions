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
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import hcl2

MAX_MANIFEST = 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024
DEADLINE = 180
_NAME = r"[a-z0-9][a-z0-9_-]{0,63}"
_DIGEST = r"[0-9a-f]{64}"
_QUERY_KEYS = frozenset(
    "sp sv sr spr se rscd rsct skoid sktid skt ske sks skv sig jwt "
    "response-content-disposition response-content-type".split()
)


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


def checked_manifest(value: Any) -> dict[str, Any]:
    """Bind provider identities and module graphs to a single engine policy."""
    exact_keys(value, {"schema", "engine", "providers", "modules"})
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
            re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?", provider["version"]),
            "provider-version",
        )
        require(re.fullmatch(_NAME + "_" + _NAME, provider["platform"]), "provider-platform")
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


class PinnedHTTPS(http.client.HTTPSConnection):
    """Connect directly to a checked IP, retaining certificate and SNI hostname checks."""

    def __init__(self, host: str, deadline: float) -> None:
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=remaining(deadline), context=self.tls_context)
        self.deadline = deadline

    def connect(self) -> None:
        """Avoid proxies, CONNECT tunnels and a second DNS resolution."""
        addresses = public_addresses(self.host)
        family, address = addresses[0]
        raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(remaining(self.deadline))
            raw.connect(address)
            raw.settimeout(remaining(self.deadline))
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


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
                require(
                    sum(len(k) + len(v) for k, v in response.getheaders()) <= 32768,
                    "response-headers",
                )
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


def zip_files(data: bytes, *, limit: int) -> dict[str, bytes]:
    """Read regular archive entries into memory; never extract to the host filesystem."""
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
            with archive.open(entry) as stream:
                content = stream.read(limit + 1)
            require(len(content) == entry.file_size, "archive-size")
            result[name] = content
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
        require(
            not any(
                part.startswith(".") for part in name.split("/") if part != ".terraform.lock.hcl"
            ),
            "module-hidden",
        )
        require(
            not name.endswith((".tfstate", ".tfstate.backup", ".tfvars", ".tfvars.json")),
            "module-state",
        )
        require(b"\x00" not in data, "module-text")
        texts[name] = data.decode("utf-8")
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
        counterpart = name.replace(".tofu", ".tf") if is_tofu else name.replace(".tf", ".tofu")
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


def prepare(manifest: dict[str, Any], output: Path) -> str:
    """Write a fresh preparation directory; the CLI publishes it only on complete success."""
    checked_manifest(manifest)
    deadline = time.monotonic() + DEADLINE
    identity = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    output.mkdir()
    (output / "mirror").mkdir()
    (output / "modules").mkdir()
    receipt: dict[str, Any] = {
        "schema": 1,
        "engine": manifest["engine"],
        "policy_sha256": identity,
        "providers": [],
        "modules": [],
    }
    total = 0
    for kind in ("providers", "modules"):
        for item in manifest[kind]:
            data = fetch(item["artifact"], deadline, max_bytes=MAX_TOTAL - total)
            total += len(data)
            require(total <= MAX_TOTAL, "total-size")
            if kind == "providers":
                # Validate container structure before giving any archive to a guest installer.
                zip_files(data, limit=MAX_TOTAL)
                provider_type = item["source"].split("/")[-1]
                name = (
                    f"terraform-provider-{provider_type}_{item['version']}_{item['platform']}.zip"
                )
                target = output / "mirror" / item["source"] / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                record = {key: item[key] for key in ("source", "version", "platform")}
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
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return identity


def load_manifest(data: bytes) -> dict[str, Any]:
    """Refuse duplicate JSON keys and oversized policy documents."""
    require(len(data) <= MAX_MANIFEST, "manifest-size")

    return checked_manifest(unique_json(data, "manifest-duplicate"))


def main() -> None:
    """Isolate blocking DNS/HTTP/archive work behind a parent-enforced deadline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.worker:
            manifest = load_manifest(sys.stdin.buffer.read(MAX_MANIFEST + 1))
            prepare(manifest, args.output)
            return
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
                    str(Path(__file__).resolve()),
                    "--worker",
                    "--output",
                    str(prepared),
                ],
                input=data,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=DEADLINE,
                check=False,
            )
            require(result.returncode == 0, "preparation-failed")
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
