"""Read public registry metadata and record HTTPS redirect hosts, without running providers.

    python docs/sandbox/research/terraform-egress-probe.py --output evidence.json

The fixture versions are intentionally fixed. Signed redirect query strings are never retained.
This measures dependency URLs, not whether a particular sandbox proxy can enforce the allowlist.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PROVIDERS = (
    ("hashicorp/random", "3.7.2"),
    ("hashicorp/azurerm", "4.0.0"),
    ("hashicorp/azuread", "3.0.2"),
    ("Azure/azapi", "2.0.1"),
)
REGISTRIES = ("registry.terraform.io", "registry.opentofu.org")


class Redirects(HTTPRedirectHandler):
    """Retain host/status only; redirected release assets contain expiring signed URLs."""

    def __init__(self) -> None:
        self.hops: list[dict[str, object]] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.hops.append(
            {
                "status": code,
                "from": urlsplit(req.full_url).hostname,
                "to": urlsplit(newurl).hostname,
            }
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, metadata: bool = False) -> tuple[dict, bytes, dict]:
    """Read bounded metadata or one archive byte and close the response immediately."""
    redirects = Redirects()
    opener = build_opener(redirects)
    headers = {"User-Agent": "maf-extensions-egress-research"}
    if not metadata:
        headers["Range"] = "bytes=0-0"
    record: dict = {"host": urlsplit(url).hostname, "path": urlsplit(url).path}
    try:
        with opener.open(Request(url, headers=headers), timeout=30) as response:
            record.update(
                status=response.status,
                final_host=urlsplit(response.url).hostname,
                redirects=redirects.hops,
            )
            return record, response.read(2 * 1024 * 1024 if metadata else 1), dict(response.headers)
    except HTTPError as exc:
        record.update(status=exc.code, redirects=redirects.hops)
        return record, b"", {}


def provider(registry: str, name: str, version: str) -> dict:
    """Inspect the package, checksum, and detached-signature endpoints for one provider."""
    url = f"https://{registry}/v1/providers/{name}/{version}/download/linux/amd64"
    record, body, _ = fetch(url, metadata=True)
    result = {"registry": registry, "provider": name, "version": version, "metadata": record}
    if body:
        data = json.loads(body)
        result["artifacts"] = {
            key: fetch(urljoin(url, data[key]))[0]
            for key in ("download_url", "shasums_url", "shasums_signature_url")
            if data.get(key)
        }
    return result


def module(registry: str) -> dict:
    """Read a pinned Azure AVM module's download instruction and inspect its GitHub archive."""
    name, version = "Azure/avm-res-resources-resourcegroup/azurerm", "0.2.0"
    record, body, headers = fetch(
        f"https://{registry}/v1/modules/{name}/{version}/download", metadata=True
    )
    # OpenTofu prefers its JSON location response; Terraform uses X-Terraform-Get.
    source = (
        json.loads(body)["location"]
        if body
        else next((v for k, v in headers.items() if k.lower() == "x-terraform-get"), "")
    )
    result = {
        "registry": registry,
        "module": name,
        "version": version,
        "metadata": record,
        "source": source,
    }
    # Independently probe the archive alternative. The returned git:: source uses Git;
    # this archive redirect does not establish that Git clones need the archive host.
    if source.startswith("git::https://github.com/"):
        parsed = urlsplit(source.removeprefix("git::"))
        ref = parsed.query.removeprefix("ref=")
        repository = parsed.path.removesuffix(".git")
        result["github_archive"] = fetch(f"https://github.com{repository}/archive/{ref}.tar.gz")[0]
    return result


def main() -> None:
    """Collect the fixed public fixtures and write sanitized evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(provider, registry, name, version)
            for registry in REGISTRIES
            for name, version in PROVIDERS
        ]
        providers = [future.result() for future in futures]
    result = {
        "observed_at": datetime.now(UTC).isoformat(),
        "scope": "public dependency metadata and HTTPS redirect hosts; no provider execution",
        "discovery": {
            registry: json.loads(
                fetch(f"https://{registry}/.well-known/terraform.json", metadata=True)[1]
            )
            for registry in REGISTRIES
        },
        "providers": providers,
        "modules": [module(registry) for registry in REGISTRIES],
        "engine_archives": [
            fetch(url)[0]
            for url in (
                "https://releases.hashicorp.com/terraform/1.16.2/terraform_1.16.2_linux_amd64.zip",
                "https://github.com/opentofu/opentofu/releases/download/v1.12.6/"
                "tofu_1.12.6_linux_amd64.zip",
            )
        ],
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
