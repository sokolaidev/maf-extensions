"""Fail closed on incomplete CLI reports and separate validity from formatting."""

import json
import re
from dataclasses import dataclass
from typing import Any, cast

from ._spec import TerraformEngine

MAX_REPORT_BYTES = 1024 * 1024
MAX_FORMAT_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class ReportOutcome:
    """A rendered report, and what it says about the run as a whole.

    ``ran`` says whether the operation reached a verdict. When it did, ``valid`` means the
    configuration passed validation, or the formatter changed at least one file. It is
    meaningless unless ``ran``. Fixed refusal text lives in ``reason``; engine detail stays
    in ``output``.
    """

    output: str
    ran: bool
    valid: bool
    reason: str = ""

    @property
    def text(self) -> str:
        """The legacy report, combining the fixed reason with untrusted engine output."""
        return self.reason + self.output


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


def _envelope(raw: bytes, engine: TerraformEngine, limit: int) -> dict[str, Any]:
    if len(raw) > limit:
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
    if "error" not in envelope:
        raise ValueError("missing launcher error status")
    return envelope


def render_format_report(
    raw: bytes, engine: TerraformEngine, staged: dict[str, str], *, hidden: bool = False
) -> str:
    """The rendered formatting report alone, for a caller that does not separate the parts."""
    return format_outcome(raw, engine, staged, hidden=hidden).text


def format_outcome(
    raw: bytes, engine: TerraformEngine, staged: dict[str, str], *, hidden: bool = False
) -> ReportOutcome:
    """Return only complete changed files from the manifest; hidden names suppress all prose.

    ``valid`` carries "the formatter changed something" here: the run reached an answer, and
    that answer is whether any file differs.
    """
    envelope = _envelope(raw, engine, MAX_FORMAT_BYTES)
    if envelope.get("mode") != "format":
        raise ValueError("wrong launcher mode")
    if envelope["error"] is not None:
        return ReportOutcome(
            "",
            False,
            False,
            reason=(
                "Formatting INCOMPLETE: the launcher failed or exceeded its time/output bound. "
                "No formatted files returned; try a smaller complete manifest."
            ),
        )
    phases = _mapping(envelope.get("phases"))
    if set(phases) != {"fmt"}:
        raise ValueError("unexpected formatting phases")
    fmt = _phase(phases["fmt"])
    if fmt["exit_code"] != 0 or fmt["stderr"]:
        detail = "" if hidden else f"\n{fmt['stdout']}\n{fmt['stderr']}"
        return ReportOutcome(
            detail,
            False,
            False,
            reason="Formatting INCOMPLETE: formatter failed; no formatted files returned.",
        )
    files = _mapping(envelope.get("formatted_files"))
    for path, content in files.items():
        if (
            path not in staged
            or not isinstance(content, str)
            or "\x00" in content
            or content == staged[path]
        ):
            raise ValueError("invalid formatted file")
        content.encode("utf-8", errors="strict")
    if hidden:
        return ReportOutcome(
            ("Formatting complete; text and locations withheld because argument names are hidden."),
            True,
            bool(files),
        )
    return ReportOutcome(
        f"{engine} {envelope['version']}: formatting complete; {len(files)} changed files.\n"
        "Formatted files (JSON path-to-text mapping):\n" + json.dumps(files, ensure_ascii=True),
        True,
        bool(files),
    )


def render_report(raw: bytes, engine: TerraformEngine, *, hidden: bool = False) -> str:
    """The rendered report alone, for a caller that does not separate the parts."""
    return report_outcome(raw, engine, hidden=hidden).text


def report_outcome(raw: bytes, engine: TerraformEngine, *, hidden: bool = False) -> ReportOutcome:
    """Validate the launcher envelope and CLI counts before rendering a verdict.

    With hidden argument names, suppress all guest prose and locations. Diagnostics can repeat
    another file's name anywhere, including source snippets and arbitrary provider messages.
    """
    envelope = _envelope(raw, engine, MAX_REPORT_BYTES)
    if envelope["error"] is not None:
        # Never render arbitrary launcher error text as a host-authored instruction.
        return ReportOutcome(
            "",
            False,
            False,
            reason=(
                "Validation INCOMPLETE: the guest launcher could not complete "
                "its bounded execution."
            ),
        )
    phases = _mapping(envelope.get("phases"))
    init = _phase(phases.get("init"))
    if init["exit_code"] != 0:
        if set(phases) != {"init"}:
            raise ValueError("phases continued after failed initialization")
        detail = "" if hidden else f"\n{init['stdout']}\n{init['stderr']}"
        return ReportOutcome(
            detail,
            False,
            False,
            reason="Validation INCOMPLETE: initialization failed; dependencies were not loaded.",
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
    return ReportOutcome(chr(10).join(lines), True, valid)
