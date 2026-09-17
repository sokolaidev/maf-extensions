"""Opt-in Docker and ACAS evidence for a baked Azure Verified Modules graph."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from maf_sandbox import CallerContext, SandboxRouter
from maf_sandbox.testing import InMemoryStore
from maf_sandbox_terraform import make_terraform_tools

_SOURCE = Path(__file__).resolve().parents[1] / "images/terraform-sandbox/runner.py"
_SPEC = importlib.util.spec_from_file_location("terraform_runner", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(runner)

PREPARED = Path(os.environ.get("MAF_TERRAFORM_AVM_DIR", "")).resolve()
DOCKER_IMAGE = os.environ.get("MAF_TERRAFORM_AVM_IMAGE", "")
ACAS_IMAGE = os.environ.get("MAF_TERRAFORM_AVM_ACAS_IMAGE", "")
ACAS_ENDPOINT = os.environ.get("ACAS_SANDBOX_ENDPOINT", "")
NETWORK = "Azure/avm-res-network-virtualnetwork/azurerm"
CONSTRAINTS = {"azapi": "~> 2.4, ~> 2.12", "modtm": "~> 0.3", "random": "~> 3.5, ~> 3.6"}
BACKENDS = [
    pytest.param(
        "docker",
        marks=pytest.mark.skipif(
            not DOCKER_IMAGE or not (PREPARED / "receipt.json").is_file(),
            reason="needs MAF_TERRAFORM_AVM_IMAGE and MAF_TERRAFORM_AVM_DIR",
        ),
    ),
    pytest.param(
        "acas",
        marks=pytest.mark.skipif(
            not ACAS_IMAGE or not ACAS_ENDPOINT or not (PREPARED / "receipt.json").is_file(),
            reason="needs MAF_TERRAFORM_AVM_ACAS_IMAGE, ACAS_SANDBOX_* and MAF_TERRAFORM_AVM_DIR",
        ),
    ),
]
PASSED = "validation PASS (0 errors, 0 warnings); formatting PASS."
UNLOADED = "Validation INCOMPLETE: initialization failed; dependencies were not loaded."
NO_REGISTRY = "does not provide a modules service"
REGISTRY = "/opt/maf-terraform/registry"
# ACAS denies at a TLS-terminating proxy, so only retrieved registry content counts as reach.
EGRESS_PROBE = (
    "import urllib.request\n"
    "try:\n"
    " url = 'https://registry.terraform.io/.well-known/terraform.json'\n"
    " body = urllib.request.urlopen(url, timeout=15).read(4096)\n"
    "except Exception:\n"
    " body = b''\n"
    "print('connected' if b'modules.v1' in body else 'refused')\n"
)


def network_module(version: str = "0.22.2", source: str = NETWORK) -> str:
    return f"""module "network" {{
  source        = "{source}"
  version       = "{version}"
  location      = "westeurope"
  parent_id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/example"
  address_space = ["10.0.0.0/16"]
  subnets = {{
    workload = {{
      name             = "workload"
      address_prefixes = ["10.0.1.0/24"]
    }}
  }}
}}
"""


def removal(case: str, receipt: dict[str, Any]) -> str | None:
    """The baked dependency a case deletes in the guest after acquire and before staging."""
    if case == "missing-provider":
        return "/opt/maf-terraform/mirror/registry.terraform.io/azure/azapi"
    if case != "missing-child":
        return None
    network = next(
        item
        for item in receipt["registry_modules"]
        if item["source"].endswith(NETWORK) and item["version"] == "0.22.2"
    )
    return f"{REGISTRY}/{network['graph']['.']['interfaces']['registry']}"


def package_hash(archive: Path) -> str:
    """Terraform's h1 hash of an unpacked provider package."""
    with zipfile.ZipFile(archive) as bundle:
        lines = "".join(
            f"{hashlib.sha256(bundle.read(name)).hexdigest()}  {name}\n"
            for name in sorted(item.filename for item in bundle.infolist() if not item.is_dir())
        )
    return "h1:" + base64.b64encode(hashlib.sha256(lines.encode()).digest()).decode()


def lock_file(receipt: dict[str, Any], *, broken: bool) -> str:
    blocks = []
    for name, constraint in CONSTRAINTS.items():
        provider = max(
            (
                item
                for item in receipt["providers"]
                if item["source"].endswith("/" + name)
                and runner.satisfies(item["version"], constraint)
            ),
            key=lambda item: tuple(int(part) for part in item["version"].split(".")),
        )
        archive = (
            PREPARED
            / "mirror"
            / provider["source"]
            / f"terraform-provider-{name}_{provider['version']}_{provider['platform']}.zip"
        )
        digest = "h1:" + base64.b64encode(bytes(32)).decode() if broken else package_hash(archive)
        blocks.append(
            f'provider "{provider["source"]}" {{\n'
            f'  version     = "{provider["version"]}"\n'
            f'  constraints = "{constraint}"\n'
            f'  hashes = [\n    "{digest}",\n  ]\n}}\n'
        )
    return "\n".join(blocks)


def project(case: str, receipt: dict[str, Any]) -> tuple[dict[str, str], str]:
    root = "root"
    files = {"root/main.tf": network_module()}
    if case == "constraint":
        files["root/main.tf"] = network_module("~> 0.22")
    elif case == "incompatible-version":
        files["root/main.tf"] = network_module("0.0.1")
    elif case == "unbaked-module":
        files["root/main.tf"] = network_module("5.3.0", "Azure/network/azurerm")
    elif case == "local-wrapper":
        files = {
            "root/main.tf": 'module "wrapper" {\n  source = "../wrapper"\n}\n',
            "wrapper/main.tf": network_module(),
        }
    elif case in ("correct-lock", "wrong-lock"):
        files["root/.terraform.lock.hcl"] = lock_file(receipt, broken=case == "wrong-lock")
    return files, root


async def create_backend(name: str) -> Any:
    if name == "docker":
        from maf_sandbox_docker import DockerSandboxBackend, DockerSandboxConfig

        return await DockerSandboxBackend.create(DockerSandboxConfig())
    from maf_sandbox_acas import AcasSandboxBackend, AcasSandboxConfig

    return AcasSandboxBackend(
        AcasSandboxConfig(
            endpoint=ACAS_ENDPOINT,
            subscription_id=os.environ.get("ACAS_SANDBOX_SUBSCRIPTION_ID", ""),
            resource_group=os.environ.get("ACAS_SANDBOX_RESOURCE_GROUP", ""),
            sandbox_group=os.environ.get("ACAS_SANDBOX_GROUP", ""),
            registry=os.environ.get("ACAS_SANDBOX_REGISTRY", ""),
        )
    )


@pytest.mark.parametrize("backend_name", BACKENDS)
@pytest.mark.parametrize(
    "case,expected",
    [
        ("pinned", (PASSED,)),
        ("constraint", (PASSED,)),
        ("local-wrapper", (PASSED,)),
        ("correct-lock", (PASSED,)),
        ("incompatible-version", (UNLOADED, NO_REGISTRY, 'module "network"')),
        ("unbaked-module", (UNLOADED, NO_REGISTRY, 'module "network"')),
        ("missing-child", (UNLOADED, NO_REGISTRY, 'module "interfaces"')),
        ("missing-provider", (UNLOADED, "Failed to query available provider packages")),
        ("wrong-lock", (UNLOADED, "Failed to install provider")),
    ],
)
def test_avm_graph_validates_offline(backend_name, case, expected, monkeypatch):
    async def scenario() -> None:
        receipt = json.loads((PREPARED / "receipt.json").read_text(encoding="utf-8"))
        files, root = project(case, receipt)
        store = InMemoryStore(files.copy())
        scope = "terraform-avm-1270-" + uuid.uuid4().hex
        context = CallerContext(
            current_scope=lambda: scope,
            current_thread_id=lambda: "live",
            list_files=InMemoryStore.list,
        )
        backend = await create_backend(backend_name)
        router = SandboxRouter([backend], min_isolation=backend.isolation)
        acquire = backend.acquire
        instances: list[str] = []

        async def checked_acquire(key, spec):
            sandbox = await acquire(key, spec)
            instances.append(sandbox.instance_id)
            if backend_name == "docker":
                inspection = subprocess.run(
                    ["docker", "inspect", sandbox.instance_id],
                    capture_output=True,
                    check=True,
                    timeout=10,
                )
                assert json.loads(inspection.stdout)[0]["HostConfig"]["NetworkMode"] == "none"
            probe = await sandbox.exec(
                ["/usr/local/bin/python3", "-I", "-c", EGRESS_PROBE],
                working_directory="/tmp",
                timeout=30,
            )
            assert (probe.exit_code, probe.stdout_bytes.strip()) == (0, b"refused")
            if (path := removal(case, receipt)) is not None:
                removed = await sandbox.exec(
                    ["rm", "-rf", path], working_directory="/tmp", timeout=30
                )
                assert removed.exit_code == 0
            return sandbox

        monkeypatch.setattr(backend, "acquire", checked_acquire)
        image = DOCKER_IMAGE if backend_name == "docker" else ACAS_IMAGE
        tool = make_terraform_tools(
            router, store, "live", context, engine="terraform", image=image
        )[0]
        try:
            result = await tool.func(files=list(files), root_module=root)
            text = "\n".join(item.text or "" for item in result)
            assert all(part in " ".join(text.split()) for part in expected), text
            assert store.files == files
            assert len(instances) == 1
            if backend_name == "docker":
                inspected = subprocess.run(
                    ["docker", "inspect", instances[0]], capture_output=True, timeout=10
                )
                assert inspected.returncode != 0
        finally:
            await router.dispose_scope(scope, "live")
            if backend_name == "acas":
                await backend.aclose()

    asyncio.run(scenario())


@pytest.mark.skipif(not DOCKER_IMAGE, reason="needs MAF_TERRAFORM_AVM_IMAGE")
def test_egress_probe_connects_where_the_network_is_open():
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "bridge",
            DOCKER_IMAGE,
            "python3",
            "-I",
            "-c",
            EGRESS_PROBE,
        ],
        capture_output=True,
        timeout=60,
    )
    assert (result.returncode, result.stdout.strip()) == (0, b"connected")


LINK_PROBE = (
    "import importlib.util, json, os, tempfile\n"
    "from pathlib import Path\n"
    "call = Path(tempfile.mkdtemp())\n"
    "(call / 'project').mkdir()\n"
    "(call / 'project' / 'main.tf').write_text(%(main)r)\n"
    "spec = importlib.util.spec_from_file_location('runner', '/opt/maf-terraform/runner.py')\n"
    "runner = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(runner)\n"
    "os.chdir(call)\n"
    "result = runner.execute('terraform', '.', 300)\n"
    "providers = call / '.runner' / 'data' / 'providers'\n"
    "copied = links = outside = 0\n"
    "for path in providers.rglob('*') if providers.is_dir() else ():\n"
    "    if path.is_symlink():\n"
    "        links += 1\n"
    "        outside += 0 if str(path.resolve()).startswith('/opt/maf-terraform/mirror') else 1\n"
    "    elif path.is_file():\n"
    "        copied += 1\n"
    "print(json.dumps({'error': result['error'], 'init': result['phases'].get('init', {}).get("
    "'exit_code'), 'copied': copied, 'links': links, 'outside': outside}))\n"
)


@pytest.mark.skipif(
    not DOCKER_IMAGE or not (PREPARED / "receipt.json").is_file(),
    reason="needs MAF_TERRAFORM_AVM_IMAGE and MAF_TERRAFORM_AVM_DIR",
)
def test_prepared_providers_link_into_the_mirror_instead_of_copying():
    receipt = json.loads((PREPARED / "receipt.json").read_text(encoding="utf-8"))
    provider = receipt["providers"][0]
    main = (
        "terraform {\n  required_providers {\n"
        f'    {provider["source"].split("/")[-1]} = {{ source = "{provider["source"]}", '
        f'version = "{provider["version"]}" }}\n  }}\n}}\n'
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            DOCKER_IMAGE,
            "python3",
            "-I",
            "-c",
            LINK_PROBE % {"main": main},
        ],
        capture_output=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr.decode()
    observed = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert observed["error"] is None, observed
    assert observed["init"] == 0, observed
    assert observed["copied"] == 0, observed
    assert observed["links"] >= 1 and observed["outside"] == 0, observed
