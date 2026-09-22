"""Bicep configuration and the package-owned vocabulary of trusted summaries."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from types import MappingProxyType
from typing import Any, cast

FILE_REFERENCES = tuple(f"files[{position}]" for position in range(64))
_LEVELS = {value: value for value in ("error", "warning", "note", "none")}
_SUMMARY_LIMIT = 128
_UNATTRIBUTED = "unattributed"


@dataclass(frozen=True, slots=True)
class BicepCatalog:
    """A config snapshot and package-owned IDs captured before the tool runs."""

    config: str
    rules: Mapping[str, str]


def load_catalog(config: str | None = None) -> BicepCatalog:
    """Keep trusted IDs from package resources while selecting the host's config."""
    resources = files("maf_sandbox_bicep")
    packaged_config = resources.joinpath("bicepconfig.json").read_text(encoding="utf-8")
    rules: dict[str, Any] = json.loads(packaged_config)["analyzers"]["core"]["rules"]
    codes: list[str] = json.loads(
        resources.joinpath("compiler_codes.json").read_text(encoding="utf-8")
    )
    if (
        not rules
        or any(not re.fullmatch(r"[a-z][a-z0-9-]+", rule) for rule in rules)
        or not codes
        or any(not re.fullmatch(r"BCP[0-9]{3,}", code) for code in codes)
        or len(codes) != len(set(codes))
    ):
        raise ValueError("invalid packaged Bicep catalog")
    if config is not None:
        config = _validated_host_config(config, rules)
    return BicepCatalog(
        config if config is not None else packaged_config,
        MappingProxyType({value: value for value in (*rules, *codes)}),
    )


def _validated_host_config(config: str, packaged_rules: Mapping[str, Any]) -> str:
    """Snapshot JSON whose linter rule IDs belong to the packaged vocabulary."""
    try:
        selected = json.loads(config)
    except json.JSONDecodeError as exc:
        raise ValueError("Bicep config must be valid JSON") from exc
    if not isinstance(selected, dict):
        raise ValueError("Bicep config must be a JSON object")

    section = cast(dict[str, Any], selected)
    for name in ("analyzers", "core", "rules"):
        child = section.get(name, {})
        if not isinstance(child, dict):
            raise ValueError(f"Bicep config {name} must be a JSON object")
        section = cast(dict[str, Any], child)
    unknown = sorted(section.keys() - packaged_rules.keys())
    if unknown:
        raise ValueError(f"Bicep config names unknown linter rule(s): {', '.join(unknown)}")

    try:
        return json.dumps(selected, allow_nan=False, separators=(",", ":"))
    except ValueError as exc:
        raise ValueError("Bicep config must contain only JSON values") from exc


def diagnostic_summary(
    diagnostics: Iterable[Mapping[str, Any]],
    catalog: BicepCatalog,
    staged: Mapping[str, str],
    *,
    guest_call_directory: str = "",
) -> tuple[str, ...]:
    """Select bounded, ordered facts; paths, messages and unknown IDs never supply output text."""
    selected: set[tuple[str, str, str]] = set()
    unknown = unattributed = truncated = present = False
    for diagnostic in diagnostics:
        present = True
        rule = catalog.rules.get(diagnostic.get("rule", ""))
        level = _LEVELS.get(diagnostic.get("level", ""))
        if rule is None or level is None:
            unknown = True
            continue
        locations = diagnostic.get("locations", [])
        references: set[str] = set()
        for location in locations:
            path = location.get("file", "").removeprefix("file://")
            reference = staged.get(path)
            if reference is None and guest_call_directory and path.startswith("/"):
                # The backend owns the absolute base; the complete call subtree is ours.
                _, found, relative = path.rpartition("/" + guest_call_directory + "/")
                if found:
                    reference = staged.get(guest_call_directory + "/" + relative)
            if reference is None:
                unattributed = True
                references.add(_UNATTRIBUTED)
            else:
                references.add(reference)
        if not references:
            unattributed = True
            references.add(_UNATTRIBUTED)
        selected.update((reference, rule, level) for reference in references)
        if len(selected) > _SUMMARY_LIMIT:
            # Keep the same subset regardless of diagnostic order or duplicate phases.
            selected = set(sorted(selected)[:_SUMMARY_LIMIT])
            truncated = True
    if not present:
        return ()
    return (
        json.dumps(
            {
                "type": "bicep_diagnostics",
                "diagnostics": [
                    {"file": reference, "rule": rule, "severity": level}
                    for reference, rule, level in sorted(selected)
                ],
                "unrecognized_diagnostics": unknown,
                "unattributed_locations": unattributed,
                "truncated": truncated,
            },
            separators=(",", ":"),
        ),
    )
