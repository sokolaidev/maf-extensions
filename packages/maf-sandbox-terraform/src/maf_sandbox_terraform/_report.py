"""Fail closed on incomplete CLI reports and separate validity from formatting."""

import json
import re
from typing import Any, cast

from ._spec import TerraformEngine

MAX_REPORT_BYTES = 1024 * 1024


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate keys instead of allowing the final value to replace a verdict."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _json(text: str) -> dict[str, Any]:
    value: Any = json.loads(text, object_pairs_hook=_object, parse_constant=_nonfinite)
    return _mapping(value)


def _nonfinite(value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return cast(dict[str, Any], value)


def _phase(value: Any) -> dict[str, Any]:
    value = _mapping(value)
    if (
        type(value.get("exit_code")) is not int
        or not isinstance(value.get("stdout"), str)
        or not isinstance(value.get("stderr"), str)
    ):
        raise ValueError("invalid phase")
    return value


def render_report(raw: bytes, engine: TerraformEngine, *, hidden: bool = False) -> str:
    """Validate the launcher envelope and CLI counts before rendering a verdict.

    With hidden argument names, suppress all guest prose and locations. Diagnostics can repeat
    another file's name anywhere, including source snippets and arbitrary provider messages.
    """
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("report exceeded the output bound")
    envelope = _json(raw.decode("utf-8", errors="strict"))
    if (
        type(envelope.get("protocol")) is not int
        or envelope["protocol"] != 1
        or envelope.get("engine") != engine
        or not isinstance(envelope.get("version"), str)
        or not re.fullmatch(r"\d+\.\d+\.\d+", envelope["version"])
    ):
        raise ValueError("unsupported launcher or engine identity")
    if envelope.get("error") is not None:
        # Never render arbitrary launcher error text as a host-authored instruction.
        return "Validation INCOMPLETE: the guest launcher could not complete its bounded run."
    phases = _mapping(envelope.get("phases"))
    init = _phase(phases.get("init"))
    if init["exit_code"] != 0:
        if set(phases) != {"init"}:
            raise ValueError("phases continued after failed initialization")
        detail = "" if hidden else f"\n{init['stdout']}\n{init['stderr']}"
        return (
            "Validation INCOMPLETE: initialization failed; dependencies were not loaded." + detail
        )
    if set(phases) != {"init", "validate", "fmt"}:
        raise ValueError("incomplete phase set")
    validation = _phase(phases["validate"])
    fmt = _phase(phases["fmt"])
    if validation["stderr"]:
        raise ValueError("validation returned unexpected unstructured stderr")
    result = _json(validation["stdout"])
    version = result.get("format_version")
    diagnostics = result.get("diagnostics")
    valid = result.get("valid")
    errors = result.get("error_count")
    warnings = result.get("warning_count")
    if (
        not isinstance(version, str)
        or not re.fullmatch(r"1\.\d+", version)
        or type(valid) is not bool
        or type(errors) is not int
        or type(warnings) is not int
        or not isinstance(diagnostics, list)
        or validation["exit_code"] != (0 if valid else 1)
        or valid != (errors == 0)
    ):
        raise ValueError("unsupported or inconsistent validation verdict")
    counted = {"error": 0, "warning": 0}
    lines: list[str] = []
    for item in cast(list[Any], diagnostics):
        diagnostic = _mapping(item)
        if (
            diagnostic.get("severity") not in counted
            or not isinstance(diagnostic.get("summary"), str)
            or not isinstance(diagnostic.get("detail"), str)
        ):
            raise ValueError("invalid diagnostic")
        severity = diagnostic["severity"]
        counted[severity] += 1
        if not hidden:
            # JSON preserves locations/snippets without trusting optional schema extensions.
            lines.append(json.dumps(diagnostic, ensure_ascii=True))
    if counted != {"error": errors, "warning": warnings}:
        raise ValueError("inconsistent diagnostic counts")
    if fmt["exit_code"] not in (0, 3):
        formatting = "INCOMPLETE (formatter failed)"
    else:
        formatting = "PASS" if fmt["exit_code"] == 0 else "CHANGES REQUIRED"
    lines.insert(
        0,
        f"{engine} {envelope['version']}: validation {'PASS' if valid else 'FAIL'} "
        f"({errors} errors, {warnings} warnings); formatting {formatting}.",
    )
    if not hidden and (fmt["stdout"] or fmt["stderr"]):
        lines.append(f"Formatting output:\n{fmt['stdout']}\n{fmt['stderr']}")
    if hidden:
        lines.append(
            "Guest diagnostic text and locations withheld because argument names are hidden."
        )
    return "\n".join(lines)
