"""Select image scans from build inputs, preserving full scheduled coverage."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

PROFILES = (
    "bicep",
    "bicep-prepared",
    "sbx-bicep",
    "diagram",
    "drawio-sandbox",
    "drawio-export",
    "terraform-random",
    "opentofu-random",
    "terraform-prepared",
    "opentofu-prepared",
    "hyperlight",
    "egress-proxy",
)
IMAGE_INPUTS = {
    "images/bicep-sandbox/": ("bicep", "bicep-prepared", "sbx-bicep"),
    "images/sbx-template/": ("sbx-bicep",),
    "images/diagram-sandbox/": ("diagram",),
    "images/drawio-sandbox/": ("drawio-sandbox",),
    "images/drawio-export/": ("drawio-export",),
    "images/terraform-sandbox/": (
        "terraform-random",
        "opentofu-random",
        "terraform-prepared",
        "opentofu-prepared",
    ),
    "images/hyperlight-sandbox/": ("hyperlight",),
    "packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy/": ("egress-proxy",),
}
SCRIPT_INPUTS = {
    "scripts/build_bicep_prepared_image.py": ("bicep-prepared",),
    "scripts/bicep_dependencies.py": ("bicep-prepared",),
    "scripts/build_hyperlight_aks_image.py": ("hyperlight",),
    "scripts/hyperlight_image_smoke.py": ("hyperlight",),
    "scripts/check_hyperlight_image_compatibility.py": ("hyperlight",),
    "samples/experimental/hyperlight-aks/probe.py": ("hyperlight",),
}
WHEEL_PACKAGES = {"maf-sandbox", "maf-sandbox-codeact", "maf-sandbox-hyperlight"}
SHARED_INPUTS = {
    "uv.lock",
    "uv.toml",
    "pyproject.toml",
    ".python-version",
    ".dockerignore",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".github/workflows/image-security.yml",
    "tests/test_image_security_workflow.py",
    "tests/test_select_image_security.py",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, encoding="utf-8", stderr=subprocess.PIPE
    )


def _document(root: Path, revision: str, path: str) -> dict[str, Any]:
    return tomllib.loads(_git(root, "show", f"{revision}:{path}"))


def metadata_only(root: Path, base: str, head: str, paths: list[str]) -> set[str]:
    """Ignore package versions only when all other metadata and lock content agree."""
    ignored: set[str] = set()
    versions: dict[str, tuple[str, str, str]] = {}
    for path in paths:
        if not re.fullmatch(r"packages/[^/]+/pyproject\.toml", path):
            continue
        before, after = (_document(root, ref, path) for ref in (base, head))
        old = before["project"].pop("version")
        new = after["project"].pop("version")
        if (
            before == after
            and isinstance(old, str)
            and isinstance(new, str)
            and re.fullmatch(r"\d+\.\d+\.\d+", old)
            and re.fullmatch(r"\d+\.\d+\.\d+", new)
        ):
            ignored.add(path)
            versions[path.removesuffix("/pyproject.toml")] = (before["project"]["name"], old, new)
    if "uv.lock" in paths:
        before, after = (_document(root, ref, "uv.lock") for ref in (base, head))
        for entry in after.get("package", []):
            source = entry.get("source", {})
            change = versions.get(source.get("editable", ""))
            if change and source == {"editable": source["editable"]}:
                name, old, new = change
                if entry.get("name") == name and entry.get("version") == new:
                    entry["version"] = old
        if before == after:
            ignored.add("uv.lock")
    return ignored


def affected(path: str) -> tuple[str, ...]:
    """Map known build inputs; shared and unknown image tooling selects every profile."""
    if path in SHARED_INPUTS:
        return PROFILES
    if path in SCRIPT_INPUTS:
        return SCRIPT_INPUTS[path]
    for prefix, profiles in IMAGE_INPUTS.items():
        if path.startswith(prefix):
            return profiles
    if path.startswith(("images/", "scripts/")):
        return PROFILES
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "packages":
        if parts[2] == "pyproject.toml":
            return PROFILES
        if parts[2] not in {"tests", "CHANGELOG.md"} and parts[1] in WHEEL_PACKAGES:
            return ("hyperlight",)
    return ()


def select(root: Path, event: str, base: str, head: str) -> tuple[list[str], str]:
    """Return ordered profiles and a scope explanation, defaulting to full coverage on uncertainty."""
    if event in {"schedule", "workflow_dispatch"}:
        return list(PROFILES), f"Full coverage for {event}."
    if event not in {"pull_request", "push"} or not all(
        re.fullmatch(r"[0-9a-f]{40}", ref) and ref != "0" * 40 for ref in (base, head)
    ):
        return list(PROFILES), "Comparison unavailable; selecting every profile."
    try:
        paths = _git(root, "diff", "--name-only", "--no-renames", "-z", base, head, "--")
        changed = [path for path in paths.split("\0") if path]
        ignored = metadata_only(root, base, head, changed)
        chosen = {profile for path in changed if path not in ignored for profile in affected(path)}
    except (
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        return list(PROFILES), "Comparison could not be classified; selecting every profile."
    selected = [profile for profile in PROFILES if profile in chosen]
    reason = (
        "Selected profiles whose build inputs changed."
        if selected
        else "No image build inputs changed; no image build, scan or security evidence produced."
    )
    return selected, reason


def main() -> None:
    """Emit the selected matrix and explicitly identify runs producing no scan evidence."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--base", default=os.environ.get("BASE_SHA", ""))
    parser.add_argument("--head", default=os.environ.get("GITHUB_SHA", ""))
    args = parser.parse_args()
    profiles, reason = select(Path(__file__).resolve().parents[1], args.event, args.base, args.head)
    output = f"profiles={json.dumps(profiles)}\nscan={str(bool(profiles)).lower()}\n"
    print(output, end="")
    if target := os.environ.get("GITHUB_OUTPUT"):
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(output)
    summary = "### Image security selection\n\n" + reason + "\n"
    if profiles:
        summary += "\nProfiles: " + ", ".join(profiles) + ".\n"
    if target := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(summary)


if __name__ == "__main__":
    main()
