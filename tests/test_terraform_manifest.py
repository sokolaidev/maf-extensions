"""Manifest generation from the policy file, checked on the host without a network."""

from __future__ import annotations

import copy
import hashlib
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
        "https://registry.terraform.io/v1/modules/Azure/avm-res-network-virtualnetwork/azurerm": json.dumps(
            {"namespace": "Azure", "name": "avm-res-network-virtualnetwork", "provider": "azurerm"}
        ).encode(),
        "https://registry.terraform.io/v1/modules/Azure/avm-utl-interfaces/azure": json.dumps(
            {"namespace": "Azure", "name": "avm-utl-interfaces", "provider": "azure"}
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
    "engine": "terraform",
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
NETWORK_NAME = "avm-res-network-virtualnetwork-azurerm-0.22.2"
INTERFACES_NAME = "avm-utl-interfaces-azure-0.6.0"
GETS = {
    "https://registry.terraform.io/v1/modules/Azure/avm-res-network-virtualnetwork/azurerm/0.22.2/download": (
        f"git::https://github.com/Azure/terraform-azurerm-avm-res-network-virtualnetwork?ref={NETWORK_REVISION}"
    ),
    "https://registry.terraform.io/v1/modules/Azure/avm-utl-interfaces/azure/0.6.0/download": (
        f"git::https://github.com/Azure/terraform-azure-avm-utl-interfaces?ref={INTERFACES_REVISION}"
    ),
}


def generated(monkeypatch: pytest.MonkeyPatch, table: dict[str, bytes], policy=None) -> dict:
    install(monkeypatch, table, GETS)
    return generator.generate(copy.deepcopy(policy or POLICY))


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
    assert network["name"] == NETWORK_NAME
    assert network["graph"] == {
        ".": {
            "interfaces": {"registry": INTERFACES_NAME},
            "peering": {"local": "modules/peering"},
            "subnet": {"local": "modules/subnet"},
        },
        "modules/peering": {},
        "modules/subnet": {"interfaces": {"registry": INTERFACES_NAME}},
    }
    assert interfaces["graph"] == {".": {}}
    assert network["artifact"]["url"].endswith("/zip/" + NETWORK_REVISION)
    assert "excluded" not in document


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
        generated(monkeypatch, routes(), policy)


def test_a_nested_call_no_release_admits_is_refused(monkeypatch):
    table = routes()
    table[f"{REGISTRY_MODULES}/Azure/avm-utl-interfaces/azure/versions"] = json.dumps(
        {"modules": [{"versions": [{"version": "0.5.0"}, {"version": "0.6.0-rc1"}]}]}
    ).encode()
    policy = copy.deepcopy(POLICY)
    policy["registry_modules"][1]["constraint"] = "= 0.5.0"
    older = GETS[f"{REGISTRY_MODULES}/Azure/avm-utl-interfaces/azure/0.6.0/download"]
    monkeypatch.setitem(
        GETS, f"{REGISTRY_MODULES}/Azure/avm-utl-interfaces/azure/0.5.0/download", older
    )
    with pytest.raises(ValueError, match="no release satisfies"):
        generated(monkeypatch, table, policy)


def test_prereleases_are_never_pinned(monkeypatch):
    assert generator.newest_release(["2.13.0-rc1", "2.12.0"], "~> 2.12", "test") == "2.12.0"
    assert generator.newest_release(["2.13.0-rc1", "2.12.0"], "= 2.12.0", "test") == "2.12.0"
    with pytest.raises(ValueError, match="no release"):
        generator.newest_release(["2.13.0-rc1"], "= 2.13.0-rc1", "test")


@pytest.mark.parametrize(
    "arguments,message",
    [
        (None, "declared twice"),
        ({"source": None}, "dynamic source"),
        ({"source": "../outside"}, "escapes"),
        ({"source": "git::https://example.com/y"}, "unsupported source"),
        ({"source": "Azure/avm-x/azurerm//modules/sub", "version": "1.0.0"}, "does not list"),
        ({"source": "Azure/avm-utl-interfaces/azure"}, "does not pin"),
        ({"source": "Azure/avm-utl-interfaces/azure//modules/../x", "version": "0.6.0"}, "unclean"),
    ],
)
def test_calls_that_cannot_be_baked_are_refused(monkeypatch, arguments, message):
    install(monkeypatch, routes(), GETS)
    resolution = generator.Resolution(copy.deepcopy(POLICY))
    with pytest.raises(ValueError, match=message):
        resolution.edge(("registry.terraform.io/Azure/x/azurerm", "1.0.0"), ".", "x", arguments)


def test_a_subdirectory_call_resolves_to_a_release_and_entry(monkeypatch):
    table = routes()
    table[f"{REGISTRY_MODULES}/azure/avm-utl-interfaces/azure"] = table[
        f"{REGISTRY_MODULES}/Azure/avm-utl-interfaces/azure"
    ]
    install(monkeypatch, table, GETS)
    resolution = generator.Resolution(copy.deepcopy(POLICY))
    edge = resolution.edge(
        ("registry.terraform.io/Azure/x/azurerm", "1.0.0"),
        ".",
        "x",
        {"source": "azure/avm-utl-interfaces/azure//modules/x", "version": "~> 0.6.0"},
    )
    assert edge == {
        "registry": ("registry.terraform.io/Azure/avm-utl-interfaces/azure", "0.6.0"),
        "dir": "modules/x",
    }


def test_an_empty_called_directory_is_refused():
    prefix = f"terraform-x-{AZAPI_REVISION}/"
    data = archive_bytes({f"{prefix}main.tf": 'module "x" {\n  source = "./empty"\n}\n'})
    with pytest.raises(ValueError, match="no configuration"):
        generator.read_directory(data, prefix, "empty", "test")


REGISTRY_MODULES = "https://registry.terraform.io/v1/modules"


def catalog_routes() -> tuple[dict[str, bytes], dict[str, str]]:
    """A catalog of four roots: two usable, one needing an unapproved provider, one refused."""
    table = routes()
    gets = dict(GETS)
    listing = [
        ("avm-res-network-virtualnetwork", "azurerm"),
        ("avm-utl-interfaces", "azure"),
        ("avm-ptn-legacy", "azurerm"),
        ("avm-ptn-community", "azure"),
        ("avm-ptn-stateful", "azure"),
        ("avm-ptn-skipped", "azure"),
        ("unrelated", "azurerm"),
    ]
    table[f"{REGISTRY_MODULES}?namespace=Azure&limit=100&offset=0"] = json.dumps(
        {
            "modules": [
                {"namespace": "Azure", "name": name, "provider": system} for name, system in listing
            ],
            "meta": {},
        }
    ).encode()
    bodies = {
        "avm-ptn-legacy": (
            'terraform {\n  required_providers {\n    random = { source = "hashicorp/random", version = "~> 3.6" }\n  }\n}\n'
            'module "subnet" {\n  source  = "Azure/avm-res-network-virtualnetwork/azurerm//modules/subnet"\n  version = "0.21.0"\n}\n'
        ),
        "avm-ptn-community": 'resource "ephemeraltls_certificate" "x" {}\n'
        'terraform {\n  required_providers {\n    ephemeraltls = { source = "lonegunmanb/ephemeraltls" }\n  }\n}\n',
        "avm-ptn-stateful": "terraform {}\n",
        "avm-ptn-skipped": "terraform {}\n",
    }
    for name, main in bodies.items():
        revision = hashlib.sha256(name.encode()).hexdigest()[:40]
        prefix = f"terraform-azure-{name}-{revision}/"
        files = {f"{prefix}main.tf": main}
        if name == "avm-ptn-stateful":
            files[f"{prefix}terraform.tfstate"] = "{}"
        table[f"https://codeload.github.com/Azure/terraform-azure-{name}/zip/{revision}"] = (
            archive_bytes(files)
        )
        system = "azurerm" if name == "avm-ptn-legacy" else "azure"
        table[f"{REGISTRY_MODULES}/Azure/{name}/{system}"] = json.dumps(
            {"namespace": "Azure", "name": name, "provider": system}
        ).encode()
        table[f"{REGISTRY_MODULES}/Azure/{name}/{system}/versions"] = json.dumps(
            {"modules": [{"versions": [{"version": "1.0.0"}]}]}
        ).encode()
        gets[f"{REGISTRY_MODULES}/Azure/{name}/{system}/1.0.0/download"] = (
            f"git::https://github.com/Azure/terraform-azure-{name}?ref={revision}"
        )
    old_prefix = f"terraform-azurerm-avm-res-network-virtualnetwork-{'e' * 40}/"
    table[
        "https://codeload.github.com/Azure/terraform-azurerm-avm-res-network-virtualnetwork/zip/"
        + "e" * 40
    ] = archive_bytes(
        {
            f"{old_prefix}modules/subnet/main.tf": "terraform {}\n",
            f"{old_prefix}main.tf": "not read !",
        }
    )
    gets[f"{REGISTRY_MODULES}/Azure/avm-res-network-virtualnetwork/azurerm/0.21.0/download"] = (
        f"git::https://github.com/Azure/terraform-azurerm-avm-res-network-virtualnetwork?ref={'e' * 40}"
    )
    return table, gets


CATALOG_POLICY = {
    "schema": 1,
    "engine": "terraform",
    "providers": POLICY["providers"],
    "registry_modules": [],
    "catalog": {
        "namespace": "Azure",
        "prefixes": ["avm-"],
        "exclude": [
            {
                "source": "registry.terraform.io/Azure/avm-ptn-skipped/azure",
                "reason": "reviewed out",
            }
        ],
    },
}


def test_a_catalog_bakes_what_it_can_and_records_the_rest(monkeypatch):
    table, gets = catalog_routes()
    install(monkeypatch, table, gets)
    document = generator.generate(copy.deepcopy(CATALOG_POLICY))
    prep.checked_manifest(copy.deepcopy(document))
    names = [item["name"] for item in document["registry_modules"]]
    assert names == [
        "avm-ptn-legacy-azurerm-1.0.0",
        "avm-res-network-virtualnetwork-azurerm-0.21.0",
        NETWORK_NAME,
        INTERFACES_NAME,
    ]
    legacy, older = document["registry_modules"][:2]
    assert legacy["graph"] == {
        ".": {"subnet": {"registry": older["name"], "dir": "modules/subnet"}}
    }
    assert older["graph"] == {"modules/subnet": {}}
    reasons = {item["source"].split("/")[2]: item["reason"] for item in document["excluded"]}
    assert reasons.keys() == {"avm-ptn-community", "avm-ptn-stateful", "avm-ptn-skipped"}
    assert (
        "lonegunmanb/ephemeraltls, which the policy does not approve"
        in reasons["avm-ptn-community"]
    )
    assert (
        reasons["avm-ptn-stateful"]
        == "preparation refuses avm-ptn-stateful-azure-1.0.0: module-state"
    )
    assert reasons["avm-ptn-skipped"] == "reviewed out"


def test_pin_changes_name_what_moved():
    previous = {
        "providers": [{"source": "p", "version": "1.0.0"}],
        "registry_modules": [{"source": "m", "version": "1.0.0"}],
    }
    document = {
        "providers": [{"source": "p", "version": "1.1.0"}],
        "registry_modules": [{"source": "m", "version": "1.0.0"}],
        "excluded": [{"source": "x", "version": "2.0.0", "reason": "r"}],
    }
    assert generator.pin_changes(previous, document) == [
        "provider p 1.1.0: added",
        "provider p 1.0.0: removed",
        "excluded x 2.0.0",
    ]
    assert generator.pin_changes(previous, previous) == ["no pins changed"]


TOFU_RELEASE = "https://github.com/opentofu/terraform-provider-random/releases/download/v3.9.1"
OPENTOFU_POLICY = {
    "schema": 1,
    "engine": "opentofu",
    "providers": [{"address": "registry.opentofu.org/hashicorp/random", "constraint": ">= 3.7.0"}],
    "registry_modules": [],
}


def opentofu_routes() -> dict[str, bytes]:
    """Every request a provider-only OpenTofu policy makes, with canned responses."""
    filename = "terraform-provider-random_3.9.1_linux_amd64.zip"
    return {
        "https://registry.opentofu.org/v1/providers/hashicorp/random/versions": json.dumps(
            {
                "versions": [
                    {"version": "3.9.1", "platforms": [{"os": "linux", "arch": "amd64"}]},
                    {"version": "3.10.0", "platforms": [{"os": "darwin", "arch": "arm64"}]},
                ]
            }
        ).encode(),
        "https://registry.opentofu.org/v1/providers/hashicorp/random/3.9.1/download/linux/amd64": json.dumps(
            {
                "filename": filename,
                "download_url": f"{TOFU_RELEASE}/{filename}",
                "shasums_url": f"{TOFU_RELEASE}/terraform-provider-random_3.9.1_SHA256SUMS",
                "shasum": "9b" * 32,
            }
        ).encode(),
        f"{TOFU_RELEASE}/terraform-provider-random_3.9.1_SHA256SUMS": sums(filename, "9b" * 32),
        "https://api.github.com/repos/opentofu/terraform-provider-random": json.dumps(
            {"id": 691499456}
        ).encode(),
    }


def test_an_opentofu_policy_pins_from_the_opentofu_registry(monkeypatch):
    install(monkeypatch, opentofu_routes(), {})
    document = generator.generate(copy.deepcopy(OPENTOFU_POLICY))
    prep.checked_manifest(copy.deepcopy(document))
    assert document["engine"] == "opentofu"
    assert document["modules"] == [] and document["registry_modules"] == []
    assert [(item["source"], item["version"]) for item in document["providers"]] == [
        ("registry.opentofu.org/hashicorp/random", "3.9.1")
    ]
    assert document["providers"][0]["artifact"]["github_repository_id"] == "691499456"


@pytest.mark.parametrize(
    "field",
    [
        {"registry_modules": [{"source": "registry.opentofu.org/a/b/c", "constraint": "= 1.0.0"}]},
        {"catalog": {"namespace": "Azure", "prefixes": ["avm-"]}},
    ],
)
def test_registry_modules_are_refused_for_opentofu(field):
    policy = copy.deepcopy(OPENTOFU_POLICY) | field
    with pytest.raises(ValueError, match="cannot be baked for opentofu"):
        generator.Resolution(policy)


def test_an_address_on_the_other_registry_is_refused():
    policy = copy.deepcopy(OPENTOFU_POLICY)
    policy["providers"][0]["address"] = "registry.terraform.io/hashicorp/random"
    with pytest.raises(ValueError, match="not a registry.opentofu.org address"):
        generator.Resolution(policy)


@pytest.mark.parametrize("engine", [None, "terragrunt", ["opentofu"], {"name": "opentofu"}])
def test_a_policy_names_a_supported_engine(engine):
    policy = copy.deepcopy(OPENTOFU_POLICY)
    if engine is None:
        del policy["engine"]
    else:
        policy["engine"] = engine
    with pytest.raises(ValueError, match="engine"):
        generator.Resolution(policy)


@pytest.mark.parametrize("namespace", [["Azure"], {"name": "Azure"}, 7])
def test_a_catalog_namespace_that_is_not_a_name_is_refused(namespace):
    policy = copy.deepcopy(CATALOG_POLICY)
    policy["catalog"] = dict(policy["catalog"], namespace=namespace)
    with pytest.raises(ValueError, match="catalog needs a namespace"):
        generator.Resolution(policy)


def test_every_request_names_this_client(monkeypatch):
    """The OpenTofu registry answers urllib's default agent with 403."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return False

        def read(self, size):
            return b"{}"

    seen = []
    monkeypatch.setattr(
        generator, "urlopen", lambda request, timeout: seen.append(request) or Response()
    )
    generator.http_bytes("https://registry.opentofu.org/v1/providers/hashicorp/random/versions")
    generator.http_bytes("https://api.github.com/repos/opentofu/x", generator.github_headers())
    assert [request.get_header("User-agent") for request in seen] == [generator.USER_AGENT] * 2
    assert seen[1].get_header("Accept") == "application/vnd.github+json"
