"""The launcher's offline module records and provider-link refusals, checked host-side."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[1] / "images/terraform-sandbox/runner.py"
_SPEC = importlib.util.spec_from_file_location("terraform_runner", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)

NETWORK = "registry.terraform.io/Azure/network/azurerm"
SHARED = "registry.terraform.io/Azure/shared/azure"
NESTED = {"source": SHARED, "version": "0.6.0", "package": "shared", "dir": "."}
PACKAGES = [
    {
        "name": "network",
        "source": NETWORK,
        "version": "1.2.3",
        "inventories": {
            ".": [
                {"key": "shared", **NESTED},
                {
                    "key": "subnet",
                    "source": "./modules/subnet",
                    "package": "network",
                    "dir": "modules/subnet",
                },
            ],
            "modules/subnet": [{"key": "shared", **NESTED}],
        },
    },
    {"name": "shared", "source": SHARED, "version": "0.6.0", "inventories": {".": []}},
]


def test_native_calls_ignore_comments_heredocs_templates_and_nested_objects():
    text = """
# module "commented" { source = "Azure/x/azurerm" }
/* module "block_comment" {
  source = "Azure/x/azurerm"
} */
locals {
  policy = <<-EOT
    module "heredoc" {
      source = "Azure/x/azurerm"
    }
  EOT
  template = "${jsonencode({ "}" = "{" })}"
}
module "network" {
  source  = "Azure/network/azurerm" # pinned below
  version = "~> 1.2"
  tags = {
    source = "not-an-argument"
  }
  subnets = { for name in ["a"] : name => { source = "x" } }
}
module wrapper { source = "../wrapper" }
"""
    assert runner._hcl_module_calls(text) == {
        "network": {"source": "Azure/network/azurerm", "version": "~> 1.2"},
        "wrapper": {"source": "../wrapper"},
    }


@pytest.mark.parametrize(
    "body",
    [
        'source = "${var.prefix}/network/azurerm"',
        "source = local.source",
        'source = "Azure/network/azurerm" == "x"',
        'source = "Azure/\\u006eetwork/azurerm"',
        'source = "a"\n  source = "b"',
    ],
)
def test_native_sources_that_are_not_single_literals_record_nothing(body):
    assert runner._hcl_module_calls(f'module "network" {{\n  {body}\n}}\n') == {
        "network": {"source": None}
    }


@pytest.mark.parametrize(
    "text",
    ['module "network" {\n', "locals {\n  x = <<EOT\nnever closed\n}\n", 'x = "${"\n', "}\n"],
)
def test_native_syntax_this_reader_cannot_follow_is_refused(text):
    with pytest.raises(ValueError):
        runner._hcl_module_calls(text)


def test_repeated_native_labels_record_nothing():
    text = 'module "a" {\n  source = "./x"\n}\nmodule "a" {\n  source = "./y"\n}\n'
    assert runner._hcl_module_calls(text) == {"a": None}


def test_json_calls_accept_literals_and_refuse_duplicate_keys():
    document = {
        "module": {
            "a": {"source": "Azure/x/azurerm", "version": "1.0.0"},
            "b": {"source": "${var.x}"},
        }
    }
    assert runner._json_module_calls(json.dumps(document)) == {
        "a": {"source": "Azure/x/azurerm", "version": "1.0.0"},
        "b": {"source": None},
    }
    with pytest.raises(ValueError):
        runner._json_module_calls('{"module": {"a": {"source": "./x", "source": "./y"}}}')


def test_directory_calls_apply_override_files_and_drop_ambiguous_labels(tmp_path):
    (tmp_path / "main.tf").write_text(
        'module "network" {\n  source  = "Azure/network/azurerm"\n  version = "1.0.0"\n}\n'
        'module "twice" {\n  source = "./a"\n}\n'
    )
    (tmp_path / "other.tf.json").write_text('{"module": {"twice": {"source": "./b"}}}')
    (tmp_path / "versions_override.tf").write_text('module "network" {\n  version = "1.2.3"\n}\n')
    (tmp_path / "unreadable.tf").write_text('module "broken" {\n')
    (tmp_path / ".hidden.tf").write_text('module "hidden" {\n  source = "./h"\n}\n')
    (tmp_path / "notes.txt").write_text('module "text" {\n  source = "./t"\n}\n')
    assert runner.directory_module_calls(tmp_path) == {
        "network": {"source": "Azure/network/azurerm", "version": "1.2.3"}
    }


def test_records_follow_project_modules_and_expand_baked_inventories(tmp_path):
    project = tmp_path / "project"
    (project / "root").mkdir(parents=True)
    (project / "wrapper").mkdir()
    (tmp_path / "elsewhere").mkdir()
    (project / "root" / "main.tf").write_text(
        'module "direct" {\n  source  = "azure/network/azurerm"\n  version = "~> 1.2"\n}\n'
        'module "explicit" {\n  source = "Registry.Terraform.IO/Azure/shared/azure"\n}\n'
        'module "foreign" {\n  source = "example.com/Azure/network/azurerm"\n}\n'
        'module "outside" {\n  source = "../../elsewhere"\n}\n'
        'module "unbaked" {\n  source = "Azure/other/azurerm"\n}\n'
        'module "subdir" {\n  source = "Azure/network/azurerm//modules/subnet"\n}\n'
        'module "wrapper" {\n  source = "./../wrapper/"\n}\n'
    )
    (project / "wrapper" / "main.tf").write_text(
        'module "inner" {\n  source  = "Azure/network/azurerm"\n  version = "1.2.3"\n}\n'
    )
    records = runner.module_records(project / "root", project, PACKAGES)
    registry = "/opt/maf-terraform/registry"

    def network(key: str, source: str) -> list[dict[str, str]]:
        return [
            {"Key": key, "Source": source, "Version": "1.2.3", "Dir": f"{registry}/network"},
            {
                "Key": f"{key}.shared",
                "Source": SHARED,
                "Version": "0.6.0",
                "Dir": f"{registry}/shared",
            },
            {
                "Key": f"{key}.subnet",
                "Source": "./modules/subnet",
                "Dir": f"{registry}/network/modules/subnet",
            },
        ]

    assert records == [
        {"Key": "", "Source": "", "Dir": "."},
        *network("direct", "registry.terraform.io/azure/network/azurerm"),
        {"Key": "explicit", "Source": SHARED, "Version": "0.6.0", "Dir": f"{registry}/shared"},
        {
            "Key": "subdir",
            "Source": NETWORK + "//modules/subnet",
            "Version": "1.2.3",
            "Dir": f"{registry}/network/modules/subnet",
        },
        {"Key": "subdir.shared", "Source": SHARED, "Version": "0.6.0", "Dir": f"{registry}/shared"},
        {"Key": "wrapper", "Source": "../wrapper", "Dir": "../wrapper"},
        *network("wrapper.inner", NETWORK),
    ]


def test_records_stop_at_the_bound(tmp_path, monkeypatch):
    (tmp_path / "main.tf").write_text(
        "".join(
            f'module "m{index}" {{\n  source = "Azure/shared/azure"\n}}\n' for index in range(5)
        )
    )
    monkeypatch.setattr(runner, "MAX_RECORDS", 3)
    assert len(runner.module_records(tmp_path, tmp_path, PACKAGES)) == 3


def test_inventory_naming_an_unbaked_package_is_an_error(tmp_path):
    (tmp_path / "main.tf").write_text('module "n" {\n  source = "Azure/network/azurerm"\n}\n')
    broken = [PACKAGES[0]]
    with pytest.raises(ValueError, match="unbaked"):
        runner.module_records(tmp_path, tmp_path, broken)


def _versions(*versions: str) -> list[dict]:
    return [
        {
            "name": f"shared-{version}",
            "source": SHARED,
            "version": version,
            "inventories": {".": []},
        }
        for version in versions
    ]


@pytest.mark.parametrize(
    "argument,expected",
    [
        ("", "0.7.1"),
        ('version = "~> 0.6.0"\n', "0.6.4"),
        ('version = "= 0.6.0"\n', "0.6.0"),
        ('version = ">= 0.6, < 0.7"\n', "0.6.4"),
        ('version = "~> 0.8"\n', None),
        ('version = "~> 0"\n', None),
        ("version = var.pin\n", None),
    ],
)
def test_calls_record_the_newest_baked_version_their_constraint_admits(
    tmp_path, argument, expected
):
    (tmp_path / "main.tf").write_text(
        f'module "shared" {{\n  source = "Azure/shared/azure"\n  {argument}}}\n'
    )
    records = runner.module_records(tmp_path, tmp_path, _versions("0.6.0", "0.7.1", "0.6.4"))
    assert [record.get("Version") for record in records[1:]] == ([expected] if expected else [])


@pytest.mark.parametrize(
    "source",
    [
        "Azure/network/azurerm//modules/absent",
        "Azure/network/azurerm//modules/../modules/subnet",
        "Azure/network/azurerm//./modules/subnet",
    ],
)
def test_subdirectory_calls_need_a_baked_clean_directory(tmp_path, source):
    (tmp_path / "main.tf").write_text(f'module "x" {{\n  source = "{source}"\n}}\n')
    assert runner.module_records(tmp_path, tmp_path, PACKAGES) == [
        {"Key": "", "Source": "", "Dir": "."}
    ]


def test_native_reader_reads_crlf_heredocs_and_an_attribute_named_in():
    text = (
        "terraform {\r\n  required_providers {\r\n"
        '    azapi = { source = "Azure/azapi", version = "~> 2.4" }\r\n'
        '    random = "~> 3.5"\r\n  }\r\n}\r\n'
        'locals {\r\n  in = [\r\n    "a",\r\n  ]\r\n'
        '  doc = <<-EOT\r\n    "unbalanced { quote\r\n  EOT\r\n}\r\n'
        'resource "azapi_resource" this {}\r\n'
    )
    terraform, locals_, resource = runner.parse_hcl(text)
    ((_, _, providers),) = terraform[2]
    assert providers == [
        ("azapi", None, {"source": "Azure/azapi", "version": "~> 2.4"}),
        ("random", None, "~> 3.5"),
    ]
    assert [name for name, _, _ in locals_[2]] == ["in", "doc"]
    assert resource[:2] == ("resource", ["azapi_resource", "this"])


def test_objects_with_computed_keys_or_duplicates_are_not_literal():
    (_, _, value), (_, _, repeated) = runner.parse_hcl(
        'a = { (var.k) = "x" }\nb = { source = "x", source = "y" }\n'
    )
    assert value is None
    assert repeated == {"source": None}


@pytest.mark.parametrize(
    "version,constraint,expected",
    [("1.2.3", "~> 1.2", True), ("2.0.0", "~> 1.2", False), ("0.6.4", "~> 0.6.0", True)],
)
def test_launcher_constraints_follow_the_preparer(version, constraint, expected):
    assert runner.satisfies(version, constraint) is expected


@pytest.mark.parametrize(
    "version,constraint", [("1.0.0", "~> 1"), ("1.0.0-rc1", "1.0.0"), ("1.0.0", "v1")]
)
def test_launcher_refuses_constraint_syntax_it_cannot_decide(version, constraint):
    with pytest.raises(ValueError):
        runner.satisfies(version, constraint)


MIRROR_PACKAGE = (
    "/opt/maf-terraform/mirror"
    "/registry.terraform.io/hashicorp/random/3.7.2/linux_amd64"
    "/terraform-provider-random_v3.7.2"
)


def _package(data: Path) -> Path:
    return data / "providers/registry.terraform.io/hashicorp/random/3.7.2/linux_amd64"


def _link(entry: Path, target: str) -> None:
    """Create one symlink; Windows may refuse without developer mode enabled."""
    try:
        entry.symlink_to(target)
    except OSError:
        pytest.skip("Windows cannot always create symlinks")


def test_providers_installed_by_link_into_the_mirror_pass(tmp_path):
    data = tmp_path / "data"
    runner.refuse_copied_providers(data)
    _package(data).mkdir(parents=True)
    _link(_package(data) / "terraform-provider-random_v3.7.2", MIRROR_PACKAGE)
    runner.refuse_copied_providers(data)


def test_a_copied_provider_under_the_data_directory_is_refused(tmp_path):
    data = tmp_path / "data"
    _package(data).mkdir(parents=True)
    (_package(data) / "terraform-provider-random_v3.7.2").write_bytes(b"copied bytes")
    with pytest.raises(ValueError, match="copied"):
        runner.refuse_copied_providers(data)


def test_a_provider_link_pointing_outside_the_mirror_is_refused(tmp_path):
    data = tmp_path / "data"
    _package(data).mkdir(parents=True)
    _link(
        _package(data) / "terraform-provider-random_v3.7.2", "/etc/terraform-provider-random_v3.7.2"
    )
    with pytest.raises(ValueError, match="outside"):
        runner.refuse_copied_providers(data)
