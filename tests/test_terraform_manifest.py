"""Manifest generation from the policy file, checked on the host without a network."""

from __future__ import annotations

import copy
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import terraform_dependencies as prep  # noqa: E402
import terraform_manifest as generator  # noqa: E402

AZAPI_REVISION = "b" * 40
NETWORK_REVISION = "c" * 40
INTERFACES_REVISION = "d" * 40


def archive_bytes(files: dict[str, str]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, content in files.items():
            bundle.writestr(name, content)
    return output.getvalue()


def sums(filename: str, digest: str) -> bytes:
    return f"0000000000000000000000000000000000000000000000000000000000000000  other.zip\n{digest}  {filename}\n".encode()


def routes() -> dict[str, bytes]:
    """Every request the generator makes for the full policy, with canned responses."""
    prefix = f"terraform-azurerm-avm-res-network-virtualnetwork-{NETWORK_REVISION}/"
    network = archive_bytes(
        {
            f"{prefix}main.tf": (
                "terraform {\n  required_providers {\n"
                '    azapi = { source = "Azure/azapi", version = "~> 2.4, ~> 2.12" }\n'
                '    modtm = { source = "Azure/modtm", version = "~> 0.3" }\n'
                '    random = { source = "hashicorp/random", version = "~> 3.5, ~> 3.6" }\n'
                "  }\n}\n"
                'module "interfaces" {\n  source  = "Azure/avm-utl-interfaces/azure"\n  version = "= 0.6.0"\n}\n'
                'module "peering" {\n  source = "./modules/peering"\n}\n'
                'module "subnet" {\n  source = "./modules/subnet"\n}\n'
            ),
            f"{prefix}modules/peering/main.tf": "terraform {}\n",
            f"{prefix}modules/subnet/main.tf": (
                'module "interfaces" {\n  source  = "Azure/avm-utl-interfaces/azure"\n  version = "= 0.6.0"\n}\n'
            ),
            f"{prefix}README.md": "docs are not configuration\n",
        }
    )
    interfaces_prefix = f"terraform-azure-avm-utl-interfaces-{INTERFACES_REVISION}/"
    interfaces = archive_bytes({f"{interfaces_prefix}main.tf": "terraform {}\n"})
    provider = {
        "filename": "terraform-provider-azapi_2.12.0_linux_amd64.zip",
        "download_url": (
            "https://github.com/Azure/terraform-provider-azapi/releases/download/v2.12.0/"
            "terraform-provider-azapi_2.12.0_linux_amd64.zip"
        ),
        "shasums_url": "https://github.com/Azure/terraform-provider-azapi/releases/download/v2.12.0/terraform-provider-azapi_2.12.0_SHA256SUMS",
        "shasum": "7" * 64,
    }
    return {
        "https://registry.terraform.io/v1/providers/azure/azapi/versions": json.dumps(
            {
                "versions": [
                    {"version": "2.13.0-rc1", "platforms": []},
                    {"version": "2.12.0", "platforms": [{"os": "linux", "arch": "amd64"}]},
                ]
            }
        ).encode(),
        "https://registry.terraform.io/v1/providers/azure/azapi/2.12.0/download/linux/amd64": json.dumps(
            provider
        ).encode(),
        provider["shasums_url"]: sums(provider["filename"], provider["shasum"]),
        "https://api.github.com/repos/Azure/terraform-provider-azapi": json.dumps(
            {"id": 409095307}
        ).encode(),
        "https://api.github.com/repos/Azure/terraform-provider-modtm": json.dumps(
            {"id": 680040111}
        ).encode(),
        "https://github.com/Azure/terraform-provider-modtm/releases/download/v0.4.0/terraform-provider-modtm_0.4.0_SHA256SUMS": sums(
            "terraform-provider-modtm_0.4.0_linux_amd64.zip", "6" * 64
        ),
        "https://registry.terraform.io/v1/providers/azure/modtm/versions": json.dumps(
            {"versions": [{"version": "0.4.0", "platforms": [{"os": "linux", "arch": "amd64"}]}]}
        ).encode(),
        "https://registry.terraform.io/v1/providers/azure/modtm/0.4.0/download/linux/amd64": json.dumps(
            {
                "filename": "terraform-provider-modtm_0.4.0_linux_amd64.zip",
                "download_url": "https://github.com/Azure/terraform-provider-modtm/releases/download/v0.4.0/terraform-provider-modtm_0.4.0_linux_amd64.zip",
                "shasums_url": "https://github.com/Azure/terraform-provider-modtm/releases/download/v0.4.0/terraform-provider-modtm_0.4.0_SHA256SUMS",
                "shasum": "6" * 64,
            }
        ).encode(),
        "https://registry.terraform.io/v1/providers/hashicorp/random/versions": json.dumps(
            {"versions": [{"version": "3.7.2", "platforms": [{"os": "linux", "arch": "amd64"}]}]}
        ).encode(),
        "https://registry.terraform.io/v1/providers/hashicorp/random/3.7.2/download/linux/amd64": json.dumps(
            {
                "filename": "terraform-provider-random_3.7.2_linux_amd64.zip",
                "download_url": "https://releases.hashicorp.com/terraform-provider-random/3.7.2/terraform-provider-random_3.7.2_linux_amd64.zip",
                "shasums_url": "https://releases.hashicorp.com/terraform-provider-random/3.7.2/SHA256SUMS",
                "shasum": "7b" * 32,
            }
        ).encode(),
        "https://releases.hashicorp.com/terraform-provider-random/3.7.2/SHA256SUMS": sums(
            "terraform-provider-random_3.7.2_linux_amd64.zip", "7b" * 32
        ),
        "https://registry.terraform.io/v1/modules/Azure/avm-res-network-virtualnetwork/azurerm/versions": json.dumps(
            {"modules": [{"versions": [{"version": "0.21.0"}, {"version": "0.22.2"}]}]}
        ).encode(),
        "https://registry.terraform.io/v1/modules/Azure/avm-utl-interfaces/azure/versions": json.dumps(
            {"modules": [{"versions": [{"version": "0.6.0"}]}]}
        ).encode(),
        "https://codeload.github.com/Azure/terraform-azurerm-avm-res-network-virtualnetwork/zip/"
        + NETWORK_REVISION: network,
        "https://codeload.github.com/Azure/terraform-azure-avm-utl-interfaces/zip/"
        + INTERFACES_REVISION: interfaces,
    }


def install(monkeypatch: pytest.MonkeyPatch, table: dict[str, bytes], gets: dict[str, str]) -> None:
    monkeypatch.setattr(generator, "http_bytes", lambda url, headers=None: table[url])
    monkeypatch.setattr(generator, "terraform_get", lambda url: gets[url])
    monkeypatch.setattr(generator, "dry_run", lambda document: None)


POLICY = {
    "schema": 1,
    "providers": [
        {"address": "registry.terraform.io/azure/azapi", "constraint": "~> 2.12"},
        {"address": "registry.terraform.io/azure/modtm", "constraint": "= 0.4.0"},
        {"address": "registry.terraform.io/hashicorp/random", "constraint": "= 3.7.2"},
    ],
    "registry_modules": [
        {
            "source": "registry.terraform.io/Azure/avm-res-network-virtualnetwork/azurerm",
            "constraint": "= 0.22.2",
        },
        {"source": "registry.terraform.io/Azure/avm-utl-interfaces/azure", "constraint": "= 0.6.0"},
    ],
}


def generated(monkeypatch: pytest.MonkeyPatch, table: dict[str, bytes]) -> dict:
    gets = {
        "https://registry.terraform.io/v1/modules/Azure/avm-res-network-virtualnetwork/azurerm/0.22.2/download": (
            f"git::https://github.com/Azure/terraform-azurerm-avm-res-network-virtualnetwork?ref={NETWORK_REVISION}"
        ),
        "https://registry.terraform.io/v1/modules/Azure/avm-utl-interfaces/azure/0.6.0/download": (
            f"git::https://github.com/Azure/terraform-azure-avm-utl-interfaces?ref={INTERFACES_REVISION}"
        ),
    }
    install(monkeypatch, table, gets)
    return generator.generate(copy.deepcopy(POLICY))


def test_generation_resolves_pins_graphs_and_ids(monkeypatch):
    document = generated(monkeypatch, routes())
    prep.checked_manifest(document)
    assert document["providers"][0]["artifact"] == {
        "url": "https://github.com/Azure/terraform-provider-azapi/releases/download/v2.12.0/terraform-provider-azapi_2.12.0_linux_amd64.zip",
        "sha256": "7" * 64,
        "provenance": "Azure azapi v2.12.0 release SHA256SUMS matching registry download metadata",
        "github_repository_id": "409095307",
    }
    hashicorp = document["providers"][2]["artifact"]
    assert hashicorp["provenance"] == (
        "HashiCorp random 3.7.2 release SHA256SUMS matching registry download metadata"
    )
    assert "github_repository_id" not in hashicorp
    network, interfaces = document["registry_modules"]
    assert network["name"] == "avm-res-network-virtualnetwork"
    assert network["graph"] == {
        ".": {
            "interfaces": {"registry": "avm-utl-interfaces"},
            "peering": {"local": "modules/peering"},
            "subnet": {"local": "modules/subnet"},
        },
        "modules/peering": {},
        "modules/subnet": {"interfaces": {"registry": "avm-utl-interfaces"}},
    }
    assert interfaces["graph"] == {".": {}}
    assert network["artifact"]["url"].endswith("/zip/" + NETWORK_REVISION)


def test_generation_is_deterministic(monkeypatch):
    text = generator.render(generated(monkeypatch, routes())) + "\n"
    again = generator.render(generated(monkeypatch, routes())) + "\n"
    assert text == again
    assert '"graph": {".": {}}' in text
    prep.checked_manifest(json.loads(text))


def test_a_checksum_disagreement_is_refused(monkeypatch):
    table = routes()
    for url, body in list(table.items()):
        if url.endswith("terraform-provider-azapi_2.12.0_SHA256SUMS"):
            table[url] = f"{'8' * 64}  terraform-provider-azapi_2.12.0_linux_amd64.zip\n".encode()
    with pytest.raises(ValueError, match="SHA256SUMS"):
        generated(monkeypatch, table)


def test_a_call_to_an_unlisted_module_is_refused(monkeypatch):
    policy = copy.deepcopy(POLICY)
    policy["registry_modules"] = policy["registry_modules"][:1]
    with pytest.raises(ValueError, match="does not list"):
        generator.generate(policy)


def test_a_call_constraint_must_admit_the_pinned_version(monkeypatch):
    policy = copy.deepcopy(POLICY)
    policy["registry_modules"][1]["constraint"] = "= 0.5.0"
    versions = generator.module_versions
    monkeypatch.setattr(
        generator,
        "module_versions",
        lambda source: ["0.5.0", "0.6.0"] if "interfaces" in source else versions(source),
    )
    with pytest.raises(ValueError, match="requires"):
        generator.generate(policy)


def test_prereleases_are_never_pinned(monkeypatch):
    assert generator.newest_release(["2.13.0-rc1", "2.12.0"], "~> 2.12", "test") == "2.12.0"
    assert generator.newest_release(["2.13.0-rc1", "2.12.0"], "= 2.12.0", "test") == "2.12.0"
    with pytest.raises(ValueError, match="no release"):
        generator.newest_release(["2.13.0-rc1"], "= 2.13.0-rc1", "test")


def test_dynamic_remote_and_unpinned_sources_are_refused():
    prefix = f"terraform-x-{AZAPI_REVISION}/"

    def package(main: str) -> bytes:
        return archive_bytes({f"{prefix}main.tf": main})

    def graph(main: str):
        return generator.module_call_graph(package(main), prefix, "test")

    with pytest.raises(ValueError, match="dynamic"):
        graph('module "x" {\n  source = var.source\n}\n')
    with pytest.raises(ValueError, match="unsupported source"):
        graph('module "x" {\n  source = "git::https://example.com/y"\n}\n')
    with pytest.raises(ValueError, match="unsupported source"):
        graph('module "x" {\n  source = "Azure/avm-x/azurerm//modules/sub"\n}\n')
    _, calls = graph('module "x" {\n  source = "Azure/avm-x/azurerm"\n}\n')
    assert calls == [(".", "x", "registry.terraform.io/Azure/avm-x/azurerm", None)]


def test_an_empty_called_directory_is_refused():
    prefix = f"terraform-x-{AZAPI_REVISION}/"
    data = archive_bytes({f"{prefix}main.tf": 'module "x" {\n  source = "./empty"\n}\n'})
    with pytest.raises(ValueError, match="no configuration"):
        generator.module_call_graph(data, prefix, "test")
