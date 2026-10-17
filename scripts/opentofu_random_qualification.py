"""Offline consumer qualification for the prepared OpenTofu random image."""

from __future__ import annotations

import inspect
import json
from typing import Any

from terraform_avm_qualification import CONTEXT, FIELDS, expected_receipt, read

CASES = (
    "pinned",
    "correct-lock",
    "uncached-version",
    "wrong-lock",
    "missing-module",
    "wrong-registry",
    "upstream-lock",
)
SUCCESS = {"pinned", "correct-lock"}


def expected_refusal(name: str, diagnostics: str) -> bool:
    """Require the intended offline dependency refusal rather than a general failure."""
    text = " ".join(diagnostics.split())
    if name == "uncached-version":
        return "no available releases match" in text
    if name == "wrong-lock":
        return "Failed to install provider" in text and "doesn't match any of the checksums" in text
    if name == "upstream-lock":
        return "SOKOLAI provider lock verification failed: expected baked h1 checksum." in text
    if name == "missing-module":
        return "Unreadable module directory" in text
    if name == "wrong-registry":
        return (
            "Failed to query available provider packages" in text
            and "registry.terraform.io/hashicorp/random" in text
        )
    return False


_GUEST = r"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snapshot(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob("*") if p.is_file()}


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
assert metadata["engine"] == "opentofu" and metadata["profile"] == "prepared"
assert metadata["dependencies_manifest_sha256"] == summary["manifest_sha256"]
assert metadata["dependencies_policy_sha256"] == summary["policy_sha256"]
build = metadata["source_build"]
assert json.loads((install / "source-build/build.json").read_text()) == build
for name in ("go.mod", "go.sum", "LICENSE"):
    assert hashlib.sha256((install / "source-build" / name).read_bytes()).hexdigest() == build["files"][name]
assert hashlib.sha256(Path("/usr/local/bin/tofu").read_bytes()).hexdigest() == metadata["binary_sha256"] == build["files"]["tofu"]
assert hashlib.sha256((install / "licenses/LICENSE").read_bytes()).hexdigest() == build["files"]["LICENSE"]
provider, = receipt["providers"]
module, = receipt["modules"]
assert provider["source"] == "registry.opentofu.org/hashicorp/random"
module_root = install / "modules" / module["name"]
assert snapshot(module_root) == module["files"]
checks = []
for case in CASES:
    with tempfile.TemporaryDirectory(prefix="opentofu-release-") as temporary:
        call = Path(temporary)
        project = call / "project"
        project.mkdir()
        shutil.copytree(module_root, project / "modules" / module["name"])
        source = "registry.terraform.io/hashicorp/random" if case == "wrong-registry" else provider["source"]
        version = "0.0.1" if case == "uncached-version" else provider["version"]
        module_path = "absent" if case == "missing-module" else module["name"]
        (project / "main.tf").write_text(
            "terraform {\n  required_providers {\n"
            f'    random = {{ source = "{source}", version = "{version}" }}\n'
            '  }\n}\nmodule "approved" {\n'
            f'  source = "./modules/{module_path}"\n}}\n'
        )
        if case in ("correct-lock", "wrong-lock", "upstream-lock"):
            digest = "h1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" if case == "wrong-lock" else provider["h1"]
            if case == "upstream-lock":
                digest = "zh:" + provider["upstream_artifact"]["sha256"]
            lock = project / ".terraform.lock.hcl"
            lock.write_text(f'provider "{source}" {{\n  version = "{version}"\n  hashes = ["{digest}"]\n}}\n')
            lock.chmod(0o444)
        before = snapshot(project)
        os.chdir(call)
        try:
            result = runner.execute("opentofu", ".", 60)
            phases = result["phases"]
            init = phases.get("init", {})
            validate = phases.get("validate", {})
            providers = call / ".runner/data/providers"
            links = [p for p in providers.rglob("*") if p.is_symlink()]
            # OpenTofu creates an empty cache mutex beside the linked package.
            mutex = providers / provider["source"] / provider["version"] / (provider["platform"] + ".lock")
            files = [p for p in providers.rglob("*") if p.is_file() and not p.is_symlink()
                     and not (p == mutex and p.stat().st_size == 0)]
            after = snapshot(project)
            generated_lock = ".terraform.lock.hcl" not in before and ".terraform.lock.hcl" in after
            if generated_lock:
                del after[".terraform.lock.hcl"]
            module_file = call / ".runner/data/modules/modules.json"
            modules = json.loads(module_file.read_text())["Modules"] if module_file.is_file() else []
            checks.append({
                "name": case,
                "error": result["error"],
                "version": result["version"],
                "initExitCode": init.get("exit_code"),
                "validateExitCode": validate.get("exit_code"),
                "validationValid": json.loads(validate.get("stdout", "{}")).get("valid"),
                "sourceUnchanged": after == before,
                "generatedLock": generated_lock,
                "providerLinks": len(links),
                "providerCopies": len(files),
                "providersInMirror": all(p.resolve().is_relative_to(install / "mirror") for p in links),
                "moduleLoaded": any(item.get("Key") == "approved" for item in modules),
                "expectedRefusal": expected_refusal(case, init.get("stdout", "") + init.get("stderr", "")),
            })
        finally:
            os.chdir("/tmp")
print(json.dumps({"receipt": summary, "build": build, "providerBuild": provider["source_build"], "moduleFilesVerified": True, "checks": checks}))
"""


def command() -> list[str]:
    """Execute bounded probes inside the retained image with no registry access."""
    return [
        "/usr/local/bin/python3",
        "-c",
        f"FIELDS = {FIELDS!r}\nCASES = {CASES!r}\n" + inspect.getsource(expected_refusal) + _GUEST,
    ]


def evidence(output: bytes) -> dict[str, Any]:
    """Bind runtime results to the reviewed dependency policy and source build."""
    result = json.loads(output)
    if result["receipt"] != expected_receipt("dependencies.opentofu.json"):
        raise ValueError("OpenTofu receipt differs from the selected release policy")
    engine = read(CONTEXT / "image.json")["engines"]["opentofu"]
    if (
        result["build"]["source"] != engine["source_build"]
        or result["build"].get("go_version")
        != "go version " + engine["source_build"]["go_version"] + " linux/amd64"
        or result["moduleFilesVerified"] is not True
    ):
        raise ValueError("OpenTofu build or module identity differs from the selected release")
    provider = read(CONTEXT / "dependencies.opentofu.json")["providers"][0]
    if result["providerBuild"] != provider["source_build"]:
        raise ValueError("OpenTofu provider build identity differs from the selected release")
    checks = result["checks"]
    if not isinstance(checks, list) or len(checks) != len(CASES):
        raise ValueError("OpenTofu qualification cases are incomplete")
    for actual, name in zip(checks, CASES, strict=True):
        success = name in SUCCESS
        if (
            actual["name"] != name
            or actual["version"] != engine["version"]
            or actual["error"] is not None
            or actual["sourceUnchanged"] is not True
            or actual["generatedLock"] is not (name == "pinned")
            or (
                success
                and (
                    actual["initExitCode"] != 0
                    or actual["validateExitCode"] != 0
                    or actual["validationValid"] is not True
                    or actual["providerLinks"] < 1
                    or actual["providerCopies"] != 0
                    or actual["providersInMirror"] is not True
                    or actual["moduleLoaded"] is not True
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
            raise ValueError(f"OpenTofu consumer probe failed: {name}")
    return {
        "opentofuVersion": engine["version"],
        **result,
        "checks": [item | {"passed": True} for item in checks],
    }
