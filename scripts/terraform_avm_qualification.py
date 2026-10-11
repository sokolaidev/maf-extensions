"""Offline release qualification for the pinned Terraform AVM catalogue."""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any

from container_release import ROOT, read
from terraform_dependencies import policy_contract

CONTEXT = ROOT / "images/terraform-sandbox"
CASES = ("pinned", "constraint", "correct-lock", "uncached-version", "wrong-lock")
FIELDS = {
    "providers": ("source", "version", "platform"),
    "modules": ("name", "revision", "graph"),
    "registry_modules": ("name", "source", "version", "revision", "graph"),
}


def fingerprint(value: Any) -> str:
    """Match the preparer's canonical JSON identity."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def expected_receipt() -> dict[str, Any]:
    """Bind the candidate to the reviewed catalogue and effective preparation policy."""
    manifest = read(CONTEXT / "dependencies.terraform-avm.json")
    manifest_sha = fingerprint(manifest)
    return {
        "schema": 1,
        "engine": "terraform",
        "manifest_sha256": manifest_sha,
        "policy_sha256": fingerprint(
            {"manifest_sha256": manifest_sha, "contract": policy_contract()}
        ),
        "artifacts": {
            kind: fingerprint(
                [
                    {key: item[key] for key in fields}
                    | {"sha256": item["artifact"]["sha256"], "decision": "verified"}
                    for item in manifest.get(kind, [])
                ]
            )
            for kind, fields in FIELDS.items()
        },
    }


# Run inside the retained image; only small summaries cross the bounded output pipe.
_GUEST = r'''
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

install = Path("/opt/maf-terraform")
spec = importlib.util.spec_from_file_location("runner", install / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
receipt = json.loads((install / "dependencies.json").read_text())
metadata = json.loads((install / "engine.json").read_text())
summary = {key: receipt[key] for key in ("schema", "engine", "manifest_sha256", "policy_sha256")}
summary["artifacts"] = {
    kind: fingerprint([{key: item[key] for key in (*fields, "sha256", "decision")} for item in receipt[kind]])
    for kind, fields in FIELDS.items()
}
assert metadata["engine"] == "terraform" and metadata["profile"] == "prepared"
assert metadata["dependencies_manifest_sha256"] == summary["manifest_sha256"]
assert metadata["dependencies_policy_sha256"] == summary["policy_sha256"]
source = """module "network" {
  source        = "Azure/avm-res-network-virtualnetwork/azurerm"
  version       = "VERSION"
  location      = "westeurope"
  parent_id     = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/example"
  address_space = ["10.0.0.0/16"]
  subnets = {
    workload = {
      name             = "workload"
      address_prefixes = ["10.0.1.0/24"]
    }
  }
}
"""
constraints = {"azapi": "~> 2.4, ~> 2.12", "modtm": "~> 0.3", "random": "~> 3.5, ~> 3.6"}
checks = []
for case in CASES:
    with tempfile.TemporaryDirectory(prefix="avm-release-") as temporary:
        call = Path(temporary)
        project = call / "project"
        project.mkdir()
        version = "~> 0.22" if case == "constraint" else "0.0.0" if case == "uncached-version" else "0.22.2"
        (project / "main.tf").write_text(source.replace("VERSION", version))
        if case in ("correct-lock", "wrong-lock"):
            blocks = []
            for name, constraint in constraints.items():
                provider = max(
                    (item for item in receipt["providers"] if item["source"].endswith("/" + name)
                     and runner.satisfies(item["version"], constraint)),
                    key=lambda item: tuple(int(part) for part in item["version"].split(".")),
                )
                digest = "h1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" if case == "wrong-lock" else provider["h1"]
                blocks.append(
                    f'provider "{provider["source"]}" {{\n'
                    f'  version = "{provider["version"]}"\n'
                    f'  constraints = "{constraint}"\n'
                    f'  hashes = ["{digest}"]\n}}\n'
                )
            (project / ".terraform.lock.hcl").write_text("\n".join(blocks))
        before = {path.name: path.read_bytes() for path in project.iterdir()}
        os.chdir(call)
        try:
            result = runner.execute("terraform", ".", 60)
            phases = result["phases"]
            init = phases.get("init", {})
            validate = phases.get("validate", {})
            diagnostics = init.get("stdout", "") + init.get("stderr", "")
            providers = call / ".runner/data/providers"
            links = [path for path in providers.rglob("*") if path.is_symlink()]
            files = [path for path in providers.rglob("*") if path.is_file() and not path.is_symlink()]
            module_file = call / ".runner/data/modules/modules.json"
            modules = json.loads(module_file.read_text())["Modules"] if module_file.is_file() else []
            checks.append({
                "name": case,
                "error": result["error"],
                "version": result["version"],
                "initExitCode": init.get("exit_code"),
                "validateExitCode": validate.get("exit_code"),
                "validationValid": json.loads(validate.get("stdout", "{}")).get("valid"),
                "sourceUnchanged": all((project / name).read_bytes() == content for name, content in before.items()),
                "providerLinks": len(links),
                "providerCopies": len(files),
                "providersInMirror": all(path.resolve().is_relative_to(install / "mirror") for path in links),
                "nestedModuleLoaded": any(item.get("Key") == "network.interfaces" for item in modules),
                "expectedRefusal": expected_refusal(case, diagnostics),
            })
        finally:
            os.chdir("/tmp")
print(json.dumps({"receipt": summary, "checks": checks}))
'''


def expected_refusal(name: str, diagnostics: str) -> bool:
    """Match Terraform diagnostics independently of terminal line wrapping."""
    text = " ".join(diagnostics.split())
    if name == "uncached-version":
        return "does not provide a modules service" in text
    if name == "wrong-lock":
        return "Failed to install provider" in text and "doesn't match any of the checksums" in text
    return False


def command() -> list[str]:
    """Run representative validation and refusal probes without registry access."""
    return [
        "/usr/local/bin/python3",
        "-c",
        f"FIELDS = {FIELDS!r}\nCASES = {CASES!r}\n" + inspect.getsource(expected_refusal) + _GUEST,
    ]


def evidence(output: bytes) -> dict[str, Any]:
    """Refuse receipt drift, incomplete checks and false-positive validation."""
    result = json.loads(output)
    if result["receipt"] != expected_receipt():
        raise ValueError("Terraform AVM receipt differs from the selected release policy")
    checks = result["checks"]
    version = read(CONTEXT / "image.json")["engines"]["terraform"]["version"]
    if not isinstance(checks, list) or len(checks) != len(CASES):
        raise ValueError("Terraform AVM qualification cases are incomplete")
    for actual, name in zip(checks, CASES, strict=True):
        success = name not in {"uncached-version", "wrong-lock"}
        if (
            actual["name"] != name
            or actual["version"] != version
            or actual["error"] is not None
            or actual["sourceUnchanged"] is not True
            or (
                success
                and (
                    actual["initExitCode"] != 0
                    or actual["validateExitCode"] != 0
                    or actual["validationValid"] is not True
                    or actual["providerLinks"] < 3
                    or actual["providerCopies"] != 0
                    or actual["providersInMirror"] is not True
                    or actual["nestedModuleLoaded"] is not True
                )
            )
            or (
                not success
                and (
                    actual["initExitCode"] != 1
                    or actual["validateExitCode"] is not None
                    or actual["validationValid"] is not None
                    or actual["expectedRefusal"] is not True
                )
            )
        ):
            raise ValueError(f"Terraform AVM consumer probe failed: {name}")
    return {
        "terraformVersion": version,
        **result,
        "checks": [item | {"passed": True} for item in checks],
    }
