"""Check live ACAS image prerequisites without creating a sandbox or importing an image.

References come from the source under test. An existing import proves presence, not that
its snapshot contains the current contents of a registry tag.
"""

from __future__ import annotations

import argparse
import ast
import os
import shlex
from collections.abc import Mapping
from pathlib import Path

from azure.containerapps.sandbox import SandboxGroupClient
from azure.identity import AzureCliCredential
from maf_sandbox_acas._images import (
    disk_image_base,
    names_a_prebuilt_image,
    qualify_image_reference,
)

_CONFIG = {
    "endpoint": "ACAS_SANDBOX_ENDPOINT",
    "subscription_id": "ACAS_SANDBOX_SUBSCRIPTION_ID",
    "resource_group": "ACAS_SANDBOX_RESOURCE_GROUP",
    "sandbox_group": "ACAS_SANDBOX_GROUP",
}
_CODEACT_SAMPLES = ("03_acas_codeact", "14_acas_codeact_files", "15_acas_codeact_host_tools")


def _image_constant(path: Path, name: str, env: Mapping[str, str]) -> str:
    """Read a literal or an os.environ.get default without importing a sample or test suite."""
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    values = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            values.append(statement.value)
    if len(values) == 1:
        value = values[0]
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
            return value.value
        if (
            isinstance(value, ast.Call)
            and ast.unparse(value.func) == "os.environ.get"
            and len(value.args) == 2
            and not value.keywords
            and all(
                isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in value.args
            )
        ):
            variable, default = (str(ast.literal_eval(arg)) for arg in value.args)
            resolved = env.get(variable, default)
            if resolved:
                return resolved
    raise ValueError(f"Cannot read {name} from {path}; update the image preflight for this source.")


def required_images(
    root: Path, package: str, env: Mapping[str, str]
) -> tuple[dict[str, list[str]], list[str]]:
    """Collect only images used by the selected live jobs, with their consumers and skips."""
    all_jobs = package in ("", "maf-sandbox", "maf-sandbox-acas")
    bicep = all_jobs or package == "maf-sandbox-bicep"
    codeact = all_jobs or package == "maf-sandbox-codeact"
    required: dict[str, list[str]] = {}
    skipped: list[str] = []
    missing = []
    if bicep:
        missing.extend(
            name for name in ("ACAS_SANDBOX_REGISTRY", "BICEP_SANDBOX_IMAGE") if not env.get(name)
        )
    if missing:
        raise ValueError("the live-verify environment is missing: " + ", ".join(missing))

    def add(reference: str, consumer: str) -> None:
        if not names_a_prebuilt_image(reference):
            reference = qualify_image_reference(env.get("ACAS_SANDBOX_REGISTRY", ""), reference)
        required.setdefault(reference, []).append(consumer)

    if bicep:
        add(env["BICEP_SANDBOX_IMAGE"], "sample-01 / acas-e2e" if all_jobs else "sample-01")
    if codeact:
        for sample in _CODEACT_SAMPLES:
            add(
                _image_constant(root / "samples" / sample / "agent.py", "CODEACT_IMAGE", env),
                sample,
            )
    if all_jobs:
        add(
            _image_constant(
                root / "packages/maf-sandbox-acas/tests/test_acas_e2e.py", "_PREBUILT", env
            ),
            "acas-e2e prebuilt",
        )
        nonroot = env.get("ACAS_SANDBOX_NONROOT_IMAGE", "")
        if nonroot:
            add(nonroot, "acas-e2e non-root")
        else:
            skipped.append("ACAS_SANDBOX_NONROOT_IMAGE is unset; the optional non-root leg skips.")
    return required, skipped


def _import_command(reference: str) -> str:
    return (
        "uv run python packages/maf-sandbox-acas/scripts/import_disk_image.py "
        '--endpoint "$ACAS_SANDBOX_ENDPOINT" --subscription "$ACAS_SANDBOX_SUBSCRIPTION_ID" '
        '--resource-group "$ACAS_SANDBOX_RESOURCE_GROUP" --group "$ACAS_SANDBOX_GROUP" '
        f"--image {shlex.quote(reference)}"
    )


def check_images(
    client: SandboxGroupClient, required: Mapping[str, list[str]]
) -> tuple[list[str], list[str]]:
    """Read each needed namespace once and report every missing or ambiguous reference."""
    imported: dict[str, set[str]] = {}
    prebuilt: set[str] = set()
    if any(not names_a_prebuilt_image(reference) for reference in required):
        for image in client.list_disk_images():
            base = disk_image_base(image)
            if base and image.id:
                imported.setdefault(base, set()).add(image.id)
    if any(names_a_prebuilt_image(reference) for reference in required):
        prebuilt = {image.name for image in client.list_public_disk_images() if image.name}

    present: list[str] = []
    failures: list[str] = []
    for reference, consumers in required.items():
        asset = f"{reference!r} ({', '.join(consumers)})"
        if names_a_prebuilt_image(reference):
            if reference not in prebuilt:
                available = ", ".join(sorted(prebuilt)) or "nothing; the catalogue is empty"
                failures.append(f"Missing prebuilt image {asset}. Service catalogue: {available}.")
                continue
        else:
            matches = imported.get(reference, set())
            if not matches:
                failures.append(
                    f"Missing imported disk image {asset}. Import it with: `{_import_command(reference)}`. "
                    "For private-registry credentials, see packages/maf-sandbox-acas/scripts/README.md."
                )
                continue
            if len(matches) > 1:
                failures.append(
                    f"Ambiguous imported disk image {asset}: {len(matches)} snapshots match. "
                    "Push and import a new build tag, then update the configured reference."
                )
                continue
        present.append(f"Present: {asset}")
    return present, failures


def main(argv: list[str] | None = None) -> int:
    """Check the selected live jobs and append their result to the Actions summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--package", default="", help="release package; empty checks every ACAS job"
    )
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args(argv)
    messages: list[str] = []
    failures: list[str] = []
    try:
        required, messages = required_images(args.source_root, args.package, os.environ)
        if required:
            config = {key: os.environ.get(variable, "") for key, variable in _CONFIG.items()}
            missing = [variable for key, variable in _CONFIG.items() if not config[key]]
            if missing:
                raise ValueError("the live-verify environment is missing: " + ", ".join(missing))
            with AzureCliCredential() as credential:
                with SandboxGroupClient(
                    endpoint=config["endpoint"],
                    credential=credential,
                    subscription_id=config["subscription_id"],
                    resource_group=config["resource_group"],
                    sandbox_group=config["sandbox_group"],
                ) as client:
                    present, failures = check_images(client, required)
                    messages.extend(present)
        else:
            messages.append("No ACAS jobs selected.")
    except Exception as exc:
        failures.append(f"ACAS image preflight could not complete: {exc}")

    messages.append("Presence does not verify snapshot freshness or guest behavior.")
    for message in messages:
        print(message)
    for failure in failures:
        escaped = failure.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error::{escaped} See docs/maintainers.md.")
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as summary:
            summary.write("## ACAS image preflight\n\n")
            for message in [*messages, *failures]:
                summary.write(f"- {message}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
