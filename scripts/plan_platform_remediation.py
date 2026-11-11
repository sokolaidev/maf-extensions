"""Map Bicep and APK findings to bounded build inputs or ordinary rebuild checks."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import vendor_fix_evidence
from bicep_source import policy
from container_image_inputs import build_inputs, inventory
from image_scan_policy import BICEP_ROUTES
from plan_go_remediation import digest, evidence, locations, semver
from select_image_security import PROFILES

BICEP = "images/bicep-sandbox"
NUGET = f"{BICEP}/dependencies.csproj"
APK_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?-r(0|[1-9][0-9]*)")
RUNTIME_RECIPES = {
    "azure-pricing": "images/azure-pricing-sandbox/Dockerfile",
    "bicep": f"{BICEP}/source.Dockerfile",
    "bicep-avm": f"{BICEP}/source.Dockerfile",
    "bicep-docker-sbx": f"{BICEP}/source.Dockerfile",
    "graphviz": "images/graphviz-sandbox/Dockerfile",
    "document": "images/document-sandbox/Dockerfile",
    "document-export": "images/document-export/Dockerfile",
    "drawio": "images/drawio-sandbox/Dockerfile",
    "drawio-export": "images/drawio-export/Dockerfile",
    "shell": "images/shell-sandbox/Dockerfile",
    "antivirus": "images/antivirus-sandbox/Dockerfile",
    **dict.fromkeys(
        ("terraform-random", "terraform-avm", "opentofu-random", "opentofu-avm"),
        "images/terraform-sandbox/Dockerfile",
    ),
}


def inputs(root: Path) -> dict[str, str]:
    """Bind inherited recipes and compatibility policy as well as editable manifests."""
    paths = [*build_inputs(root), root / "scripts/image_scan_policy.py"]
    result = {}
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Build input escapes the checkout")
        result[path.relative_to(root).as_posix()] = digest(path)
    return result


def version(value: str, kind: str) -> tuple[int, ...]:
    """Keep automatic updates on the declared major/minor compatibility line."""
    if kind == "dotnet":
        if isinstance(value, str) and value.startswith("v"):
            raise ValueError("Unsupported NuGet version")
        return semver(value)
    match = APK_VERSION.fullmatch(value)
    if match is None:
        raise ValueError("Unsupported APK version")
    return tuple(int(part or "0") for part in match.groups())


def target(installed: str, kind: str, fix: dict[str, Any]) -> str | None:
    """Choose a stable advertised fix within the installed compatibility family."""
    current = version(installed, kind)
    candidates = []
    if fix.get("state") != "fixed":
        return None
    for value in fix.get("versions") or []:
        try:
            parsed = version(value, kind)
        except (ValueError, TypeError):
            continue
        if parsed[:2] == current[:2] and parsed > current:
            candidates.append((parsed, value))
    return max(candidates)[1] if candidates else None


def apk_inventory(component: dict[str, Any]) -> bool:
    """Accept the APK database paths used before and after usr-merge."""
    paths = locations(component)
    return bool(paths) and set(paths) <= {"/lib/apk/db/installed", "/usr/lib/apk/db/installed"}


def runtime_base(root: Path, profile: str) -> str | None:
    """Resolve only the reviewed Wolfi or Alpine runtime recipe."""
    name = RUNTIME_RECIPES.get(profile)
    if name is None:
        return None
    references = {
        ref
        for ref in inventory(root / name)
        if ref.startswith(("cgr.dev/chainguard/wolfi-base:latest@", "alpine:3.24@"))
    }
    return next(iter(references)) if len(references) == 1 else None


def acquire_plan(root: Path, directories: list[Path]) -> dict[str, Any]:
    """Acquire vendor feeds alongside the authenticated monitor inputs."""
    if not directories or len({path.parent for path in directories}) != 1:
        raise ValueError("Expected one retained monitoring directory")
    vendor = vendor_fix_evidence.acquire(directories[0].parent.parent / "vendor-security")
    return plan(root, directories, vendor=vendor)


def plan(
    root: Path, directories: list[Path], *, vendor: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Retain unsupported findings; absence of an edit never establishes a clean image."""
    hashes = inputs(root)
    source = policy(root / BICEP / "source.json")
    findings, records = [], []
    updates: dict[tuple[str, str], dict[str, Any]] = {}
    affected = set()
    for directory in directories:
        identity, matches, retained = evidence(directory)
        profile = identity["profile"]
        if profile not in PROFILES:
            raise ValueError("Unknown platform remediation profile")
        records.append({**identity, "files": retained})
        for match in matches:
            component, vulnerability = match["artifact"], match["vulnerability"]
            kind, name = component["type"], component["name"]
            row: dict[str, Any] = {
                "profile": profile,
                "imageId": identity["imageId"],
                "id": vulnerability["id"],
                "component": name,
                "type": kind,
                "installed": component["version"],
                "guestPaths": locations(component),
                "fix": vulnerability.get("fix") or {},
                "status": "unsupported-component",
            }
            findings.append(row)
            if kind not in {"dotnet", "apk"}:
                continue
            try:
                fixed = target(component["version"], kind, row["fix"])
                if kind == "apk" and vendor is not None:
                    declarations = vendor_fix_evidence.fixes(vendor, component, row["id"])
                    vendor_target = target(
                        component["version"],
                        kind,
                        {"state": "fixed", "versions": [d["fixedVersion"] for d in declarations]},
                    )
                    if vendor_target and (
                        fixed is None or version(vendor_target, kind) >= version(fixed, kind)
                    ):
                        fixed = vendor_target
                        row["vendorFix"] = next(
                            d for d in declarations if d["fixedVersion"] == fixed
                        )
            except (ValueError, TypeError):
                row["status"] = "unsupported-version"
                continue
            if fixed is None:
                row["status"] = "no-compatible-fix"
                continue
            row["target"] = fixed
            if kind == "dotnet":
                if (
                    profile not in BICEP_ROUTES
                    or name not in source["packages"]
                    or not row["guestPaths"]
                    or any(path != "/usr/local/bin/bicep" for path in row["guestPaths"])
                ):
                    row["status"] = "unmapped-bicep-component"
                    continue
                row["publishedRoute"] = identity.get("bicepRoute", "upstream")
                row["route"] = BICEP_ROUTES[profile]
                if row["route"] != "source":
                    row["status"] = "upstream-binary-upgrade-required"
                    continue
                previous = source["packages"][name]
                if version(previous, kind)[:2] != version(fixed, kind)[:2]:
                    row["status"] = "incompatible-source-policy"
                    continue
                path = NUGET
                affected.update(BICEP_ROUTES)
            else:
                base = runtime_base(root, profile)
                if base is None or not re.fullmatch(r"[a-z0-9][a-z0-9+_.-]*", name):
                    row["status"] = "unsupported-vendor"
                    continue
                vendor_name = "wolfi" if base.startswith("cgr.dev/") else "alpine"
                if not re.fullmatch(
                    rf"pkg:apk/{vendor_name}/{re.escape(name)}@{re.escape(component['version'])}(?:[?#].*)?",
                    component.get("purl", ""),
                ) or not apk_inventory(component):
                    row["status"] = "unmapped-vendor-package"
                    continue
                row["base"] = base
                path = RUNTIME_RECIPES[profile]
                text = (root / path).read_text(encoding="utf-8")
                pins = set(re.findall(rf"(?<![\w/-]){re.escape(name)}=([^\s\\]+)", text))
                if len(pins) > 1:
                    row["status"] = "ambiguous-package-pin"
                    continue
                previous = next(iter(pins)) if pins else None
                # The Sandboxes layer can install APKs absent from its parent recipe.
                row["profiles"] = sorted(
                    item
                    for item, recipe in RUNTIME_RECIPES.items()
                    if recipe == path
                    and (profile != "bicep-docker-sbx" or previous is not None or item == profile)
                )
                if previous is None:
                    if "apk upgrade --no-cache" not in text:
                        row["status"] = "unsupported-rebuild-recipe"
                        continue
                    row["status"] = "rebuild-required"
                    affected.update(row["profiles"])
                    continue
                try:
                    compatible = version(previous, kind)[:2] == version(fixed, kind)[:2]
                except ValueError:
                    compatible = False
                if not compatible:
                    row["status"] = "incompatible-package-pin"
                    continue
                affected.update(row["profiles"])
            if version(previous, kind) >= version(fixed, kind):
                row["status"] = "source-pin-sufficient"
                row["target"] = previous
                continue
            row["status"] = "update"
            row["path"] = path
            key = path, name
            update = {
                "path": path,
                "component": name,
                "type": kind,
                "before": previous,
                "after": fixed,
            }
            if key not in updates or version(fixed, kind) > version(updates[key]["after"], kind):
                updates[key] = update

    def stable(value: dict[str, Any]) -> str:
        return json.dumps(value, sort_keys=True)

    return {
        "schemaVersion": 1,
        "kind": "platform",
        "inputs": hashes,
        "evidence": sorted({stable(row): row for row in records}.values(), key=stable),
        "findings": sorted({stable(row): row for row in findings}.values(), key=stable),
        "updates": [updates[key] for key in sorted(updates)],
        "affectedProfiles": sorted(affected),
        **({"vendorEvidence": vendor["receipt"]} if vendor is not None else {}),
    }


def proposal(root: Path, expected: dict[str, Any]) -> dict[str, bytes]:
    """Render only exact package version replacements, preserving every other byte."""
    if inputs(root) != expected["inputs"]:
        raise ValueError("Platform remediation inputs changed")
    files: dict[str, bytes] = {}
    for update in expected["updates"]:
        path, name, before, after = (
            update[key] for key in ("path", "component", "before", "after")
        )
        kind = update["type"]
        old, new = version(before, kind), version(after, kind)
        if new[:2] != old[:2] or new <= old:
            raise ValueError("Unsafe platform package update")
        raw = files[path] if path in files else (root / path).read_bytes()
        if kind == "dotnet" and path == NUGET:
            old_bytes = f'Include="{name}" Version="{before}"'.encode()
            new_bytes = f'Include="{name}" Version="{after}"'.encode()
            if raw.count(old_bytes) != 1:
                raise ValueError("Expected one build-consumed NuGet pin")
        elif kind == "apk" and path in RUNTIME_RECIPES.values():
            old_bytes, new_bytes = f"{name}={before}".encode(), f"{name}={after}".encode()
            pattern = rb"(?<![\w/-])" + re.escape(old_bytes) + rb"(?=[\s\\]|$)"
            replaced, count = re.subn(pattern, lambda _: new_bytes, raw)
            if not count:
                raise ValueError("Expected a build-consumed APK pin")
            files[path] = replaced
            continue
        else:
            raise ValueError("Unsupported platform proposal path")
        files[path] = raw.replace(old_bytes, new_bytes)
    return files


def file_hashes(files: dict[str, bytes]) -> dict[str, str]:
    """Bind qualification and bot ownership to the exact proposed bytes."""
    return {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
