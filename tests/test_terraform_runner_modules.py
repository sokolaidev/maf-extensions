"""The launcher's offline module records, checked on the host without an engine."""

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
PACKAGES = [
    {
        "name": "network",
        "source": NETWORK,
        "version": "1.2.3",
        "inventory": [
            {
                "key": "shared",
                "source": SHARED,
                "version": "0.6.0",
                "package": "shared",
                "dir": ".",
            },
            {
                "key": "subnet",
                "source": "./modules/subnet",
                "package": "network",
                "dir": "modules/subnet",
            },
        ],
    },
    {"name": "shared", "source": SHARED, "version": "0.6.0", "inventory": []},
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
