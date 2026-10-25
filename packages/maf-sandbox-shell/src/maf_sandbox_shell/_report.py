"""Validate complete analyzer envelopes and separate finite summaries from guest prose."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, cast

from ._spec import SEVERITIES, ShellFlavor, ShellPolicy, ShellSeverity

MAX_REPORT_BYTES = 1024 * 1024
EMPTY_INVENTORY_SHA256 = hashlib.sha256(b"{}\n").hexdigest()
IDENTITY = {
    "launcher": "maf-shell-v1",
    "bash": "5.3-r14",
    "dash": "0.5.13.5-r2",
    "shellcheck": "0.11.0",
    "powershell": "7.6.8",
    "psscriptanalyzer": "1.25.0",
}
_CATALOG = {
    "SC1090": "A dynamically selected source could not be followed.",
    "SC1091": "A referenced source could not be read.",
    "SC2034": "A variable appears unused.",
    "SC2046": "Quote command substitution to prevent word splitting.",
    "SC2086": "Quote variable expansion to prevent splitting and globbing.",
    "SC2154": "A variable is referenced without an assignment.",
    "SC2162": "Use read -r to preserve backslashes.",
    "SC2164": "Handle failure when changing directories.",
    "PSAvoidUsingCmdletAliases": "Use a command's full name instead of an alias.",
    "PSAvoidUsingWriteHost": "Choose an output stream appropriate to the caller.",
    "PSAvoidUsingInvokeExpression": "Avoid evaluating strings as PowerShell code.",
    "PSAvoidUsingPlainTextForPassword": "Use a credential type for password parameters.",
    "PSUseDeclaredVarsMoreThanAssignments": "A variable appears unused.",
    "PSUseApprovedVerbs": "Use an approved verb in a function name.",
    "PSParseError": "PowerShell could not parse the source.",
}


@dataclass(frozen=True, slots=True)
class Outcome:
    """Only a completed analysis can carry a verdict; details always remain untrusted."""

    completed: bool
    verdict: str | None
    summary: tuple[str, ...]
    detail: str


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError("nonfinite JSON value")


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return cast(dict[str, Any], value)


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("expected array")
    return cast(list[Any], value)


def _position(value: dict[str, Any]) -> None:
    if any(
        type(value.get(key)) is not int or not 0 <= value[key] <= 2**31
        for key in ("line", "column")
    ):
        raise ValueError("invalid source location")


def _diagnostic(value: Any, paths: list[str]) -> dict[str, Any]:
    item = _mapping(value)
    _position(item)
    if (
        item.get("file") not in paths
        or item.get("severity") not in SEVERITIES
        or not isinstance(item.get("rule"), str)
        or not item["rule"]
        or not isinstance(item.get("message"), str)
    ):
        raise ValueError("invalid diagnostic")
    return item


def _suppression(value: Any, flavor: ShellFlavor) -> None:
    item = _mapping(value)
    _position(item)
    if flavor == ShellFlavor.PWSH:
        if not isinstance(item.get("text"), str) or not item["text"]:
            raise ValueError("invalid suppression declaration")
        return
    ranges = _list(item.get("rules"))
    if not ranges:
        raise ValueError("empty suppression declaration")
    for value in ranges:
        span = _mapping(value)
        if any(
            type(span.get(key)) is not int or span[key] < 0 for key in ("first", "end_exclusive")
        ):
            raise ValueError("invalid suppression range")
    if item.get("scope") is not None:
        scope = _mapping(item["scope"])
        _position(_mapping(scope.get("start")))
        _position(_mapping(scope.get("end")))


def report_outcome(
    raw: bytes,
    flavor: ShellFlavor,
    paths: list[str],
    policy: ShellPolicy,
    inventory_sha256: str = EMPTY_INVENTORY_SHA256,
    *,
    hidden: bool = False,
) -> Outcome:
    """Recompute verdicts from diagnostics, honoring inline suppressions and host policy."""
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("report exceeds bound")
    envelope = _mapping(
        json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_nonfinite)
    )
    if (
        type(envelope.get("protocol")) is not int
        or envelope["protocol"] != 1
        or envelope.get("identity") != IDENTITY
        or envelope.get("flavor") != flavor
        or "error" not in envelope
    ):
        raise ValueError("unsupported launcher identity")
    if envelope["error"] is not None:
        return Outcome(
            False, None, ("Validation incomplete: the bounded analyzer did not finish.",), ""
        )
    if envelope.get("dependency_inventory_sha256") != inventory_sha256:
        raise ValueError("unexpected host dependency inventory")
    reports = [_mapping(item) for item in _list(envelope.get("files"))]
    if [item.get("path") for item in reports] != paths:
        raise ValueError("report does not cover the complete manifest")
    completed = True
    findings = False
    summaries: set[str] = set()
    details: list[dict[str, Any]] = []
    for report in reports:
        if (
            type(report.get("parsed")) is not bool
            or type(report.get("policy_conflict")) is not bool
        ):
            raise ValueError("missing analyzer completion")
        syntax = _mapping(report.get("syntax"))
        if (
            type(syntax.get("exit_code")) is not int
            or not isinstance(syntax.get("stdout"), str)
            or not isinstance(syntax.get("stderr"), str)
        ):
            raise ValueError("invalid syntax phase")
        completed &= report["parsed"] and not report["policy_conflict"] and syntax["exit_code"] == 0
        diagnostics = [_diagnostic(item, paths) for item in _list(report.get("diagnostics"))]
        suppressed = [
            _diagnostic(item, paths) for item in _list(report.get("suppressed_diagnostics"))
        ]
        declarations = _list(report.get("suppressions"))
        for declaration in declarations:
            _suppression(declaration, flavor)
        if declarations or suppressed:
            summaries.add("Inline suppressions are present; see the untrusted audit report.")
        excluded = [item for item in diagnostics if item["rule"] in policy.excluded_rules]
        effective = [item for item in diagnostics if item["rule"] not in policy.excluded_rules]
        findings |= any(
            SEVERITIES.index(ShellSeverity(item["severity"]))
            <= SEVERITIES.index(policy.minimum_severity)
            for item in effective
        )
        for item in effective:
            if item["rule"] in _CATALOG:
                index = paths.index(item["file"])
                summaries.add(
                    f"files[{index}]: {item['severity']} {item['rule']}. {_CATALOG[item['rule']]}"
                )
            else:
                summaries.add("Additional analyzer findings are available in the untrusted report.")
        details.append(
            {
                **report,
                "counts": {
                    severity: sum(item["severity"] == severity for item in effective)
                    for severity in SEVERITIES
                },
                "suppressed_counts": dict(Counter(item["severity"] for item in suppressed)),
                "suppression_declarations": len(declarations),
                "suppressed_findings": len(suppressed),
                "host_excluded_findings": len(excluded),
            }
        )
    if not completed:
        summaries.add(
            "Validation incomplete: syntax, lint or suppression auditing could not complete."
        )
    elif not findings:
        summaries.add("No unsuppressed diagnostic meets the configured threshold.")
    if policy.excluded_rules:
        summaries.add("Host rule exclusions are active.")
    return Outcome(
        completed,
        ("findings" if findings else "clean") if completed else None,
        tuple(sorted(summaries)),
        ""
        if hidden
        else json.dumps(
            {
                "files": details,
                "policy": {
                    "minimum_severity": policy.minimum_severity,
                    "excluded_rules": policy.excluded_rules,
                },
            },
            ensure_ascii=True,
        ),
    )
