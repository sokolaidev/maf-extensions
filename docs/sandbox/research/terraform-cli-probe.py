"""Probe pinned IaC CLIs in an offline disposable Linux container.

This is research over fixed fixtures, not an implementation of the proposed kind.
Supply read-only /probe/bin/{terraform/terraform,tofu/tofu} and /probe/mirror.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

RANDOM = """terraform {
  required_providers {
    random = {
      source  = "hashicorp/random"
      version = "3.7.2"
    }
  }
}
resource "random_string" "example" {
  length = 12
}
"""

FIXTURES: dict[str, tuple[str, dict[str, str]]] = {
    "local_module": (
        "root",
        {
            "root/main.tf": """variable "message" { type = string }
module "child" {
  source = "../modules/child"
  message = var.message
}
output "result" { value = module.child.result }
""",
            "modules/child/main.tf": """variable "message" { type = string }
resource "terraform_data" "value" { input = var.message }
output "result" { value = terraform_data.value.output }
""",
        },
    ),
    "json_configuration": (
        ".",
        {"main.tf.json": '{"resource":{"terraform_data":{"example":{"input":"ok"}}}}'},
    ),
    "semantic_error": (".", {"main.tf": 'output "value" { value = var.undeclared }\n'}),
    "syntax_error": (".", {"main.tf": 'resource "terraform_data" "broken" {\n'}),
    "missing_local_module": (".", {"main.tf": 'module "missing" { source = "./absent" }\n'}),
    "missing_provider": (
        ".",
        {"main.tf": RANDOM.replace('"hashicorp/random"', '"example/absent"')},
    ),
    "random_valid": (".", {"main.tf": RANDOM}),
    "random_invalid": (".", {"main.tf": RANDOM.replace("length = 12", 'length = "invalid"')}),
    "readonly_without_lock": (".", {"main.tf": RANDOM}),
    "partial_s3_backend": (
        ".",
        {"main.tf": 'terraform {\n  backend "s3" {}\n}\nresource "terraform_data" "x" {}\n'},
    ),
    "tofu_only": (".", {"main.tofu": 'output "value" { value = var.undeclared }\n'}),
    "tofu_precedence": (
        ".",
        {
            "main.tf": 'output "value" { value = var.undeclared }\n',
            "main.tofu": 'output "value" { value = "ok" }\n',
        },
    ),
    "formatting_only": (
        ".",
        {"main.tf": 'resource "terraform_data" "example" {\ninput=   "ok"\n}\n'},
    ),
    "outside_call_file": (
        ".",
        {"main.tf": 'output "value" { value = file("/tmp/terraform-research-witness") }\n'},
    ),
    "outside_call_missing": (
        ".",
        {"main.tf": 'output "value" { value = file("/tmp/terraform-research-absent") }\n'},
    ),
}


def execute(binary: str, arguments: list[str], cwd: Path, env: dict[str, str]) -> dict[str, Any]:
    """Capture one fixed-fixture command with a finite deadline."""
    result = subprocess.run(  # noqa: S603
        [binary, *arguments],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    record: dict[str, Any] = {"arguments": arguments, "exit_code": result.returncode}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        record["json"] = payload
    else:
        record["stdout"] = result.stdout[-8000:]
    if result.stderr:
        record["stderr"] = result.stderr[-8000:]
    return record


def run_case(
    binary: str, base: Path, name: str, root: str, files: dict[str, str]
) -> dict[str, Any]:
    """Initialize and validate one directory tree, retaining evidence of failed initialization."""
    call = base / name
    project = call / "project"
    for relative, content in files.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    for directory in ("home", "tmp", "data"):
        (call / directory).mkdir()
    config = call / "cli.tfrc"
    config.write_text(
        "disable_checkpoint = true\n"
        'provider_installation {\n  filesystem_mirror { path = "/probe/mirror" }\n}\n',
        encoding="utf-8",
    )
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(call / "home"),
        "TMPDIR": str(call / "tmp"),
        "TF_DATA_DIR": str(call / "data"),
        "TF_CLI_CONFIG_FILE": str(config),
        "TF_INPUT": "0",
        "TF_IN_AUTOMATION": "1",
        "CHECKPOINT_DISABLE": "1",
    }
    cwd = project / root
    init_args = ["init", "-backend=false", "-input=false", "-no-color"]
    if name in {"readonly_without_lock", "cross_engine_lock"}:
        init_args.append("-lockfile=readonly")
    initialized = execute(binary, init_args, cwd, env)
    record: dict[str, Any] = {"init": initialized}
    if initialized["exit_code"] == 0:
        trace = call / "provider-trace.log"
        validation = execute(
            binary,
            ["validate", "-json"],
            cwd,
            {**env, "TF_LOG": "TRACE", "TF_LOG_PATH": str(trace)},
        )
        record["validate"] = validation
        if trace.exists():
            trace_text = trace.read_text(encoding="utf-8")
            record["provider_process_started"] = "starting plugin:" in trace_text
            record["provider_schema_rpc"] = "GetProviderSchema" in trace_text
        record["fmt"] = execute(binary, ["fmt", "-check", "-recursive", "-no-color"], project, env)
        if name == "random_valid":
            record["readonly_after_init"] = execute(
                binary, [*init_args, "-lockfile=readonly"], cwd, env
            )
    lock = cwd / ".terraform.lock.hcl"
    if lock.exists():
        record["lock_file"] = lock.read_text(encoding="utf-8")
    record["state_files"] = [str(path.relative_to(call)) for path in call.rglob("*.tfstate*")]
    return record


def main() -> None:
    """Emit observations for both engines without deploying infrastructure."""
    Path("/tmp/terraform-research-witness").write_text("known public fixture", encoding="utf-8")
    evidence: dict[str, Any] = {"uid": os.getuid(), "engines": {}}
    with tempfile.TemporaryDirectory(prefix="terraform-cli-probe-") as temporary:
        base = Path(temporary)
        for engine, executable in (("terraform", "terraform"), ("opentofu", "tofu")):
            binary = f"/probe/bin/{executable}/{executable}"
            engine_base = base / engine
            engine_base.mkdir()
            cases = {
                name: run_case(binary, engine_base, name, root, files)
                for name, (root, files) in FIXTURES.items()
            }
            evidence["engines"][engine] = {
                "version": execute(
                    binary, ["version", "-json"], engine_base, {"CHECKPOINT_DISABLE": "1"}
                ),
                "cases": cases,
            }
        terraform_lock = evidence["engines"]["terraform"]["cases"]["random_valid"].get("lock_file")
        if terraform_lock:
            evidence["engines"]["opentofu"]["cases"]["cross_engine_lock"] = run_case(
                "/probe/bin/tofu/tofu",
                base / "opentofu",
                "cross_engine_lock",
                ".",
                {"main.tf": RANDOM, ".terraform.lock.hcl": terraform_lock},
            )
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
