"""Select image scans from build inputs, preserving full scheduled coverage."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path
from typing import Any

PROFILES = (
    "bicep",
    "bicep-avm",
    "bicep-docker-sbx",
    "graphviz",
    "drawio",
    "drawio-export",
    "terraform-random",
    "opentofu-random",
    "terraform-avm",
    "hyperlight-aks-probe",
    "egress-proxy",
)
IMAGE_INPUTS = {
    "images/egress-proxy/": ("egress-proxy",),
    "images/bicep-sandbox/": ("bicep", "bicep-avm", "bicep-docker-sbx"),
    "images/bicep-docker-sbx/": ("bicep-docker-sbx",),
    "images/sbx-template/": ("bicep-docker-sbx",),
    "images/graphviz-sandbox/": ("graphviz",),
    "images/drawio-sandbox/": ("drawio",),
    "images/drawio-export/": ("drawio-export",),
    "images/terraform-sandbox/": (
        "terraform-random",
        "opentofu-random",
        "terraform-avm",
    ),
    "images/hyperlight-sandbox/": ("hyperlight-aks-probe",),
    "packages/maf-sandbox-docker/src/maf_sandbox_docker/_proxy/": ("egress-proxy",),
}
# These exact consumer guides are not consumed by the image recipes or build helpers.
CONSUMER_GUIDES = {
    "images/egress-proxy/README.md",
    "images/bicep-sandbox/README.md",
    "images/bicep-sandbox/AVM.md",
    "images/bicep-sandbox/BICEP.md",
    "images/sbx-template/README.md",
    "images/bicep-docker-sbx/README.md",
    "images/graphviz-sandbox/README.md",
    "images/drawio-sandbox/README.md",
    "images/drawio-export/README.md",
    "images/terraform-sandbox/README.md",
    "images/terraform-sandbox/AVM.md",
    "images/terraform-sandbox/OPENTOFU.md",
    "images/terraform-sandbox/TERRAFORM.md",
    "images/hyperlight-sandbox/README.md",
}
SCRIPT_INPUTS = {
    "scripts/build_bicep_image.py": ("bicep", "bicep-avm", "bicep-docker-sbx"),
    "scripts/bicep_source.py": ("bicep", "bicep-avm", "bicep-docker-sbx"),
    "LICENSE": ("egress-proxy", "bicep-docker-sbx"),
    "scripts/egress_proxy_qualification.py": ("egress-proxy",),
    "scripts/drawio_qualification.py": ("drawio", "drawio-export"),
    "scripts/drawio_export_qualification.py": ("drawio-export",),
    "scripts/drawio_export_runtime.py": ("drawio-export",),
    "scripts/check_drawio_exports.py": ("drawio-export",),
    "scripts/check_drawio_renderer.py": ("drawio-export",),
    "scripts/check_drawio_docker.py": ("drawio", "drawio-export"),
    "scripts/terraform_dependencies.py": (
        "terraform-random",
        "terraform-avm",
        "opentofu-random",
    ),
    "scripts/terraform_avm_qualification.py": (
        "terraform-avm",
        "opentofu-random",
        "terraform-random",
    ),
    "scripts/terraform_random_qualification.py": ("terraform-random",),
    "scripts/opentofu_random_qualification.py": ("opentofu-random",),
    "scripts/build_bicep_prepared_image.py": ("bicep-avm",),
    "scripts/bicep_dependencies.py": ("bicep-avm",),
    "scripts/bicep_avm_qualification.py": ("bicep-avm",),
    "scripts/bicep_qualification.py": ("bicep", "bicep-docker-sbx"),
    "scripts/bicep_docker_sbx_qualification.py": ("bicep-docker-sbx",),
    ".github/actions/setup-sbx/action.yml": ("bicep-docker-sbx",),
    "scripts/build_hyperlight_aks_image.py": ("hyperlight-aks-probe",),
    "scripts/hyperlight_image_smoke.py": ("hyperlight-aks-probe",),
    "scripts/check_hyperlight_image_compatibility.py": ("hyperlight-aks-probe",),
    "samples/experimental/hyperlight-aks/probe.py": ("hyperlight-aks-probe",),
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
    if path in CONSUMER_GUIDES:
        return ()
    if path in SHARED_INPUTS:
        return PROFILES
    if path in SCRIPT_INPUTS:
        return SCRIPT_INPUTS[path]
    for prefix, profiles in IMAGE_INPUTS.items():
        if path.startswith(prefix):
            return profiles
    if path.startswith(("images/", "scripts/")):
        return PROFILES
    if path.startswith("packages/maf-sandbox-docker-sbx/src/"):
        return ("bicep-docker-sbx",)
    parts = path.split("/")
    if len(parts) >= 3 and parts[0] == "packages":
        if parts[2] == "pyproject.toml":
            return PROFILES
        if parts[2] not in {"tests", "CHANGELOG.md"} and parts[1] in WHEEL_PACKAGES:
            return ("hyperlight-aks-probe",)
    return ()


def _build_branches(source: str) -> tuple[str, dict[str, str]]:
    """Recognize only the literal, non-fallthrough dispatcher; keep all shared text."""
    prefix, separator, rest = source.partition('case "$1" in\n')
    if not separator or prefix != "#!/usr/bin/env bash\nset -euo pipefail\n\n":
        raise ValueError("Unknown build dispatcher")
    lines = rest.splitlines(keepends=True)
    branches: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if line == '  *) echo "Unknown image scan profile: $1" >&2; exit 2 ;;\n':
            ending = "".join(lines[index:])
            if ending != (
                '  *) echo "Unknown image scan profile: $1" >&2; exit 2 ;;\n'
                'esac\n\ncase "$1" in\n  terraform-*|opentofu-*)\n'
                "    docker run --rm -i --network none --read-only --tmpfs /tmp:rw,exec,size=256m "
                "maf-image-scan:target /usr/local/bin/python3 - < images/terraform-sandbox/test_runner.py\n"
                "    ;;\nesac\n"
            ):
                raise ValueError("Unknown dispatcher ending")
            return prefix + separator + ending, branches
        match = re.fullmatch(r"  ([a-z][a-z0-9-]*)\)\n", line)
        if match is None or match[1] in branches:
            raise ValueError("Unknown or duplicate build branch")
        profile = match[1]
        start = index
        index += 1
        while index < len(lines) and lines[index] != "    ;;\n":
            command = lines[index].removesuffix("\n")
            heredoc = re.fullmatch(r"(    docker .*) <<'([A-Z]+)'", command)
            parsed = heredoc[1] if heredoc else command
            if not parsed.startswith(("    docker ", "    uv ")) or any(
                token in parsed for token in ("`", "$(", "${", "\\", "#")
            ):
                raise ValueError("Unsupported shell syntax")
            tokens = list(shlex.shlex(parsed, posix=True, punctuation_chars=True))
            if any(token and set(token) <= set("();&|<>") for token in tokens):
                raise ValueError("Unsupported shell control operator")
            index += 1
            if heredoc:
                while index < len(lines) and lines[index] != heredoc[2] + "\n":
                    index += 1
                if index == len(lines):
                    raise ValueError("Unterminated heredoc")
                index += 1
        if index == len(lines) or index == start + 1:
            raise ValueError("Unterminated or empty build branch")
        index += 1
        branches[profile] = "".join(lines[start:index])
    raise ValueError("Missing dispatcher ending")


def _bicep_profiles(source: str) -> tuple[bytes, set[str], set[str]]:
    """Recognize a literal profile set used only in the route's membership guard."""
    tree = ast.parse(source)
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "PROFILES"
    ]
    if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Set):
        raise ValueError("Unknown Bicep profile declaration")
    value = assignments[0].value
    members = ast.literal_eval(value)
    if (
        not members
        or any(not isinstance(p, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", p) for p in members)
        or len(members) != len(value.elts)
        or value.lineno != value.end_lineno
        or value.end_col_offset is None
    ):
        raise ValueError("Invalid Bicep profile membership")
    names = [
        node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "PROFILES"
    ]
    guard = ast.dump(ast.parse("profile not in PROFILES", mode="eval").body)
    checks = [
        node for node in ast.walk(tree) if isinstance(node, ast.Compare) and ast.dump(node) == guard
    ]
    if len(names) != 2 or len(checks) != 1:
        raise ValueError("Unknown Bicep profile use")
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "validate_route"
    ]
    uses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id == "validate_route"
    ]
    shared: set[str] = set()
    if not calls or len(calls) != len(uses):
        raise ValueError("Unknown Bicep route validation use")
    for call in calls:
        if (
            len(call.args) != 2
            or call.keywords
            or not isinstance(call.args[1], ast.Constant)
            or not isinstance(call.args[1].value, str)
        ):
            raise ValueError("Unknown Bicep route validation profile")
        shared.add(call.args[1].value)
    lines = source.encode("utf-8").split(b"\n")
    line = lines[value.lineno - 1]
    lines[value.lineno - 1] = (
        line[: value.col_offset] + b"{PROFILE_MEMBERS}" + line[value.end_col_offset :]
    )
    return b"\n".join(lines), members, shared


def changed_profiles(root: Path, base: str, head: str, path: str) -> tuple[str, ...]:
    """Narrow recognized shared-file changes; uncertainty retains every affected profile."""
    if path not in {
        "images/release-profiles.json",
        "scripts/build_scan_image.sh",
        "scripts/build_bicep_image.py",
    }:
        return affected(path)
    try:
        before, after = (_git(root, "show", f"{ref}:{path}") for ref in (base, head))
        if path == "scripts/build_bicep_image.py":
            common_before, old_profiles, old_shared = _bicep_profiles(before)
            common_after, new_profiles, new_shared = _bicep_profiles(after)
            changed = old_profiles ^ new_profiles
            if common_before != common_after or changed & (old_shared | new_shared):
                return affected(path)
            return tuple(p for p in affected(path) if p in changed)
        if path == "scripts/build_scan_image.sh":
            common_before, old = _build_branches(before)
            common_after, new = _build_branches(after)
            if common_before != common_after:
                return PROFILES
            return tuple(p for p in PROFILES if old.get(p) != new.get(p))
        documents = [json.loads(text) for text in (before, after)]
        memberships = []
        for document in documents:
            if set(document) != {"schemaVersion", "platform", "registryPrefix", "profiles"}:
                raise ValueError("Unknown profile catalogue schema")
            if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
                raise ValueError("Unknown profile catalogue version")
            members = document.pop("profiles")
            if (
                not isinstance(members, list)
                or any(
                    not isinstance(p, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", p)
                    for p in members
                )
                or len(set(members)) != len(members)
            ):
                raise ValueError("Invalid profile membership")
            memberships.append(set(members))
        if documents[0] != documents[1]:
            return PROFILES
        return tuple(p for p in PROFILES if p in memberships[0] ^ memberships[1])
    except (
        subprocess.CalledProcessError,
        OSError,
        SyntaxError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ):
        return affected(path)


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
        chosen = {
            profile
            for path in changed
            if path not in ignored
            for profile in changed_profiles(root, base, head, path)
        }
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
    requested = os.environ.get("SELECTED_PROFILE", "all")
    if args.event == "workflow_dispatch" and requested != "all":
        if requested not in PROFILES:
            raise ValueError("Unknown manually selected image profile")
        profiles, reason = (
            [requested],
            "Manually selected profile; other profiles were not scanned.",
        )
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
