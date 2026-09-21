"""Generate packaged Bicep catalogs from an immutable upstream source snapshot."""

from __future__ import annotations

import argparse
import io
import json
import re
import tarfile
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PACKAGE = Path(__file__).resolve().parents[1] / "packages/maf-sandbox-bicep/src/maf_sandbox_bicep"
SCHEMA = "src/vscode-bicep/resources/configuration/bicepconfig.schema.json"
COMPILER = "src/Bicep.Core/Diagnostics/DiagnosticBuilder.cs"
RULES = "src/Bicep.Core/Analyzers/Linter/Rules/"
FILES = ("bicepconfig.json", "compiler_codes.json", "catalog_source.json")
LIMIT = 64 * 1024 * 1024
_TOKENS = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)


def uncomment(text: str) -> str:
    """Remove C# comments while retaining string literals."""
    return _TOKENS.sub(lambda m: m[0] if m[0].startswith('"') else " ", text)


def extract(source: Mapping[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Cross-check schema rule names against implementations and refuse unknown shapes."""
    schema = json.loads(source[SCHEMA])
    properties = schema["properties"]["analyzers"]["properties"]["core"]["properties"]
    rules: dict[str, Any] = {}
    for name, definition in properties["rules"]["properties"].items():
        if not re.fullmatch(r"[a-z][a-z0-9-]+", name):
            raise ValueError("unexpected linter identifier")
        settings: dict[str, Any] = {}
        levels: list[str] = []
        for part in definition["allOf"]:
            if "$ref" in part:
                match = re.fullmatch(
                    r"#/definitions/rule-def-level-(error|warning|info|off)", part["$ref"]
                )
                if match is None:
                    raise ValueError(f"unsupported level reference for {name}")
                levels.append(match[1])
            for key, value in part.get("properties", {}).items():
                if "default" in value:
                    settings[key] = value["default"]
        if len(levels) != 1:
            raise ValueError(f"expected one default level for {name}")
        rules[name] = {"level": levels[0], **settings}
    implemented: list[str] = []
    for path, text in source.items():
        if path.startswith(RULES) and path.endswith(".cs"):
            implemented.extend(
                re.findall(r'\bconst\s+string\s+Code\s*=\s*"([a-z][a-z0-9-]+)"', uncomment(text))
            )
    if not rules or set(implemented) != set(rules) or len(implemented) != len(set(implemented)):
        raise ValueError("linter implementations and configuration schema disagree")
    compiler = sorted(set(re.findall(r'"(BCP[0-9]{3,})"', uncomment(source[COMPILER]))))
    if not compiler:
        raise ValueError("no compiler diagnostic codes found")
    return dict(sorted(rules.items())), compiler


def fetch(url: str) -> bytes:
    """Bound public upstream downloads; no repository credential goes to the source host."""
    request = urllib.request.Request(url, headers={"User-Agent": "maf-extensions-bicep-catalog"})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("upstream response exceeds the download limit")
    return data


def download(release: str) -> tuple[str, dict[str, str]]:
    """Resolve a release once, then read only catalog sources from its commit archive."""
    commit = json.loads(fetch(f"https://api.github.com/repos/Azure/bicep/commits/{release}"))["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid upstream commit")
    source: dict[str, str] = {}
    total = 0
    with tarfile.open(
        fileobj=io.BytesIO(fetch(f"https://codeload.github.com/Azure/bicep/tar.gz/{commit}")),
        mode="r:gz",
    ) as archive:
        for member in archive:
            path = member.name.partition("/")[2]
            if path not in (SCHEMA, COMPILER) and not (
                path.startswith(RULES) and path.endswith(".cs")
            ):
                continue
            total += member.size
            if not member.isfile() or member.size < 0 or total > LIMIT or path in source:
                raise ValueError("invalid or oversized upstream source archive")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("missing source file")
            source[path] = stream.read().decode("utf-8-sig")
    return commit, source


def generate(
    source: Mapping[str, str], release: str, commit: str, existing: dict[str, Any]
) -> dict[str, Any]:
    """Retain local policy for existing rules and initialize new rules from upstream defaults."""
    defaults, compiler = extract(source)
    previous = existing.get("analyzers", {}).get("core", {}).get("rules", {})
    rules = {name: {**settings, **previous.get(name, {})} for name, settings in defaults.items()}
    config = {"analyzers": {"core": {"enabled": True, "rules": rules}}}
    return {
        "bicepconfig.json": config,
        "compiler_codes.json": compiler,
        "catalog_source.json": {
            "repository": "Azure/bicep",
            "release": release,
            "commit": commit,
            "upstream_rules": defaults,
        },
    }


def proposal(before: dict[str, Any], after: dict[str, Any]) -> str:
    """Describe additions, removals and upstream settings without overwriting local choices."""
    lines = [
        "## Summary",
        "",
        f"Update Bicep diagnostic catalogs from `{after['catalog_source.json']['release']}`.",
        "",
        "## Changes",
        "",
    ]
    for filename, label in (
        ("bicepconfig.json", "Linter rules"),
        ("compiler_codes.json", "Compiler codes"),
    ):
        old = before.get(filename, {})
        new = after[filename]
        if filename == "bicepconfig.json":
            old = old.get("analyzers", {}).get("core", {}).get("rules", {})
            new = new["analyzers"]["core"]["rules"]
        for verb, names in (("added", set(new) - set(old)), ("removed", set(old) - set(new))):
            lines.append(
                f"- {label} {verb}: "
                + (", ".join(f"`{name}`" for name in sorted(names)) or "none")
                + "."
            )
    old_defaults = before.get("catalog_source.json", {}).get("upstream_rules", {})
    new_defaults = after["catalog_source.json"]["upstream_rules"]
    changed = sorted(
        name
        for name in old_defaults.keys() & new_defaults.keys()
        if old_defaults[name] != new_defaults[name]
    )
    lines += [
        "- Upstream settings changed: "
        + (", ".join(f"`{name}`" for name in changed) or "none")
        + ".",
        "",
        "Existing rule settings are preserved. Review upstream setting changes before changing local policy. Catalog coverage does not upgrade the sandbox compiler.",
        "",
        "## Verification",
        "",
        "- Rule implementations match the upstream configuration schema.",
        "- Catalog and summary tests run before this proposal is opened.",
        "- Live compiler execution is not verified by this workflow.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """Update package data, or check that a pinned source regenerates it exactly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="latest")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--commit")
    parser.add_argument("--output", type=Path, default=PACKAGE)
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    release = args.release
    if release == "latest":
        release = json.loads(fetch("https://api.github.com/repos/Azure/bicep/releases/latest"))[
            "tag_name"
        ]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", release):
        parser.error("a stable vMAJOR.MINOR.PATCH release is required")
    if args.source:
        if not args.commit or not re.fullmatch(r"[0-9a-f]{40}", args.commit):
            parser.error("--source requires --commit with the upstream commit SHA")
        commit = args.commit
        source = {
            path: (args.source / path).read_text(encoding="utf-8-sig")
            for path in (SCHEMA, COMPILER)
        }
        source.update(
            {
                path.relative_to(args.source).as_posix(): path.read_text(encoding="utf-8-sig")
                for path in (args.source / RULES).glob("*.cs")
            }
        )
    else:
        commit, source = download(release)
    before = {
        name: json.loads((args.output / name).read_text(encoding="utf-8"))
        for name in FILES
        if (args.output / name).exists()
    }
    after = generate(source, release, commit, before.get("bicepconfig.json", {}))
    if (
        before.get("bicepconfig.json") == after["bicepconfig.json"]
        and before.get("compiler_codes.json") == after["compiler_codes.json"]
        and before.get("catalog_source.json", {}).get("upstream_rules")
        == after["catalog_source.json"]["upstream_rules"]
    ):
        # A compiler release alone does not change the package's diagnostic vocabulary.
        after["catalog_source.json"] = before["catalog_source.json"]
    if args.body_file:
        args.body_file.write_text(proposal(before, after), encoding="utf-8")
    changed = [name for name in FILES if before.get(name) != after[name]]
    if args.check:
        if changed:
            print("Catalog drift: " + ", ".join(changed))
        return int(bool(changed))
    args.output.mkdir(parents=True, exist_ok=True)
    for name in changed:
        (args.output / name).write_text(
            json.dumps(after[name], indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print("Updated: " + (", ".join(changed) or "nothing"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
