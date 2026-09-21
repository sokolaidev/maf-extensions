"""SARIF in, readable diagnostics out.

Pure functions over the Bicep CLI's ``--diagnostics-format sarif`` output.  No sandbox, no
Azure, no I/O — which is why the parser is the one part of this workload that is trivially
testable and has been since the first commit.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, cast

__all__ = ["RESTORE_FAILURE_RULES", "count_restore_failures", "format_diagnostics", "parse_sarif"]

# Maximum characters from a SARIF blob fed into the parser.
_SARIF_MAX_CHARS = 200_000
_SARIF_LEVELS = frozenset({"none", "note", "warning", "error"})
_MISSING = object()

#: Diagnostics that mean a module artifact never arrived: BCP190 (artifact not restored),
#: BCP191 (restore failed), BCP192 (restore failed, with the transport's reason).  When any
#: of these is present the compiler never loaded the module's types, so every check on that
#: module's inputs and outputs silently did not run — the run's other diagnostics describe a
#: DIFFERENT program than the one that would deploy.  Callers must surface that as "the
#: validation is incomplete", never fold it into an ordinary diagnostic count — a reviewer
#: once read exactly that soup, discounted it as environment noise, and PASSed files that do
#: not compile.
RESTORE_FAILURE_RULES = frozenset({"BCP190", "BCP191", "BCP192"})


def count_restore_failures(diagnostics: list[dict[str, Any]]) -> int:
    """How many of these diagnostics are module-restore failures."""
    return sum(1 for d in diagnostics if d.get("rule") in RESTORE_FAILURE_RULES)


def parse_sarif(text: str) -> list[dict[str, Any]] | None:
    """Parse Bicep SARIF analysis reports into a compact list of diagnostic dicts.

    Requires version 2.1.0, at least one analysis with a named tool driver, and explicit
    results arrays. Failed invocations and error notifications leave analysis incomplete.
    Driver defaults and invocation overrides are checked even when results are empty.
    Severity follows explicit results, invocation overrides, then driver rule defaults.
    Returns ``None`` for an incomplete or malformed report, never zero diagnostics.
    """
    try:
        data = json.loads(text[:_SARIF_MAX_CHARS])
    except (json.JSONDecodeError, ValueError):
        return None

    try:
        return _diagnostics(data)
    except (TypeError, ValueError):
        # Valid JSON does not establish the SARIF shape.
        return None


def _object(value: object) -> Mapping[str, Any]:
    """``value`` where SARIF says an object."""
    if not isinstance(value, Mapping):
        raise TypeError(f"expected an object, got {type(value).__name__}")
    return cast("Mapping[str, Any]", value)


def _array(value: object) -> list[Any]:
    """``value`` where SARIF says an array.

    Checked rather than iterated, because the wrong container is silent: ``{"runs": {}}``
    iterates as empty and would render as "no diagnostics" — a broken sandbox read as a
    clean build, which is the one answer this parser may never give by accident.
    """
    if not isinstance(value, list):
        raise TypeError(f"expected an array, got {type(value).__name__}")
    return cast("list[Any]", value)


def _level(value: object) -> str:
    if not isinstance(value, str) or value not in _SARIF_LEVELS:
        raise ValueError("expected a SARIF diagnostic level")
    return value


def _index(value: object, size: int) -> int:
    if value is _MISSING:
        return -1
    if type(value) is not int or not 0 <= value < size:
        raise ValueError("expected a SARIF array index")
    return value


def _configuration_level(value: object) -> str | None:
    configuration = _object(value)
    return _level(configuration["level"]) if "level" in configuration else None


def _descriptors(driver: Mapping[str, Any], field: str) -> list[Mapping[str, Any]]:
    descriptors: list[Mapping[str, Any]] = []
    ids: set[str] = set()
    for entry in _array(driver.get(field, [])):
        descriptor = _object(entry)
        descriptor_id = descriptor.get("id")
        if not isinstance(descriptor_id, str) or not descriptor_id or descriptor_id in ids:
            raise ValueError("expected unique nonempty driver descriptor IDs")
        _configuration_level(descriptor.get("defaultConfiguration", {}))
        ids.add(descriptor_id)
        descriptors.append(descriptor)
    return descriptors


def _overrides(
    invocation: Mapping[str, Any], field: str, descriptors: list[Mapping[str, Any]]
) -> dict[str, str]:
    levels: dict[str, str] = {}
    overridden: set[str] = set()
    for entry in _array(invocation.get(field, [])):
        override = _object(entry)
        _, target = _rule({"rule": _object(override.get("descriptor"))}, descriptors)
        if not target or target["id"] in overridden:
            raise ValueError("expected one override per driver descriptor")
        overridden.add(target["id"])
        level = _configuration_level(override.get("configuration"))
        if level is not None:
            levels[target["id"]] = level
    return levels


def _invocation_overrides(
    run: Mapping[str, Any],
    rules: list[Mapping[str, Any]],
    notifications: list[Mapping[str, Any]],
) -> list[dict[str, str]]:
    overrides: list[dict[str, str]] = []
    for entry in _array(run.get("invocations", [])):
        invocation = _object(entry)
        if invocation.get("executionSuccessful") is not True:
            raise ValueError("analysis did not succeed")
        overrides.append(_overrides(invocation, "ruleConfigurationOverrides", rules))
        notification_levels = _overrides(
            invocation, "notificationConfigurationOverrides", notifications
        )
        # SARIF 2.1.0 Appendix I: either notification channel can report incomplete analysis.
        for field in ("toolExecutionNotifications", "toolConfigurationNotifications"):
            for notification_entry in _array(invocation.get(field, [])):
                notification = _object(notification_entry)
                if not isinstance(_object(notification.get("message")).get("text"), str):
                    raise TypeError("expected notification message text")
                _, descriptor = _rule({"rule": notification.get("descriptor", {})}, notifications)
                if _effective_level(notification, descriptor, notification_levels) == "error":
                    raise ValueError("analysis reported an error notification")
    return overrides


def _rule(
    result: Mapping[str, Any], rules: list[Mapping[str, Any]]
) -> tuple[str, Mapping[str, Any]]:
    reference = _object(result.get("rule", {}))
    # Bicep uses driver rules. Do not guess a default from another tool component.
    if "toolComponent" in reference or "guid" in reference:
        raise ValueError("unsupported SARIF rule reference")
    rule_id = result.get("ruleId", reference.get("id", ""))
    if not isinstance(rule_id, str):
        raise TypeError("expected a rule ID string")
    index = _index(result.get("ruleIndex", reference.get("index", _MISSING)), len(rules))
    if reference.get("id", rule_id) != rule_id or reference.get("index", index) != index:
        raise ValueError("conflicting SARIF rule references")
    if "index" in reference:
        _index(reference["index"], len(rules))
    if index >= 0:
        rule = rules[index]
        descriptor_id = rule["id"]
        if rule_id and rule_id != descriptor_id and not rule_id.startswith(descriptor_id + "/"):
            raise ValueError("rule ID does not match its index")
        return rule_id or descriptor_id, rule
    for rule in rules:
        if rule["id"] == rule_id:
            return rule_id, rule
    if reference:
        raise ValueError("rule reference does not identify a driver rule")
    return rule_id, {}


def _effective_level(
    item: Mapping[str, Any], descriptor: Mapping[str, Any], overrides: Mapping[str, str]
) -> str:
    if "level" in item:
        return _level(item["level"])
    return (
        overrides.get(descriptor.get("id", ""))
        or _configuration_level(descriptor.get("defaultConfiguration", {}))
        or "warning"
    )


def _result_level(
    result: Mapping[str, Any], rule: Mapping[str, Any], invocations: list[dict[str, str]]
) -> str:
    provenance = _object(result.get("provenance", {}))
    # SARIF 2.1.0 section 3.48.6 associates an omitted index with a sole invocation.
    default_index = 0 if len(invocations) == 1 else _MISSING
    index = _index(provenance.get("invocationIndex", default_index), len(invocations))
    return _effective_level(result, rule, invocations[index] if index >= 0 else {})


def _diagnostics(data: Any) -> list[dict[str, Any]]:
    """The SARIF walk itself, over a blob that has parsed but is not yet known to be SARIF."""
    report = _object(data)
    if report.get("version") != "2.1.0":
        raise ValueError("expected SARIF version 2.1.0")
    runs = _array(report.get("runs"))
    if not runs:
        raise ValueError("no analysis was reported")
    diagnostics: list[dict[str, Any]] = []
    for entry in runs:
        run = _object(entry)
        driver = _object(_object(run.get("tool")).get("driver"))
        if not isinstance(driver.get("name"), str) or not driver["name"]:
            raise ValueError("expected a named tool driver")
        rules = _descriptors(driver, "rules")
        notifications = _descriptors(driver, "notifications")
        invocations = _invocation_overrides(run, rules, notifications)

        # SARIF 2.1.0 section 3.14.23: missing/null results mean analysis did not begin.
        for result_entry in _array(run.get("results")):
            result = _object(result_entry)
            rule_id, rule = _rule(result, rules)
            message = _object(result.get("message")).get("text")
            if not isinstance(message, str):
                raise TypeError("expected diagnostic message text")
            level = _result_level(result, rule, invocations)
            locs: list[dict[str, Any]] = []
            for loc_entry in _array(result.get("locations", [])):
                physical = _object(_object(loc_entry).get("physicalLocation", {}))
                region = _object(physical.get("region", {}))
                artifact = _object(physical.get("artifactLocation", {})).get("uri", "")
                if not isinstance(artifact, str):
                    # `format_diagnostics` calls `removeprefix` on it.
                    raise TypeError(f"expected a uri string, got {type(artifact).__name__}")
                locs.append(
                    {
                        "file": artifact,
                        "line": region.get("startLine"),
                        "column": region.get("startColumn"),
                    }
                )
            diagnostics.append(
                {
                    "rule": rule_id,
                    "level": level,
                    "message": message,
                    "locations": locs,
                    "help": rule.get("helpUri", ""),
                }
            )
    return diagnostics


#: What a location renders as when :func:`_renamed` could not identify it.
_UNIDENTIFIED = "an unidentified file"


def _renamed(location: str, absolute: str, rename: Mapping[str, str] | None) -> str:
    """``location`` as it may be shown, given the caller's map of every file it wrote.

    Two matches, and only one of them attributes.

    **Exact** — against the stripped location or the raw absolute path — identifies the file, so
    the caller's rendering for it is used as given, request position and all.  ``absolute`` is
    what makes this the ordinary case rather than the lucky one: Bicep is handed the path this
    call wrote and reports it back, so the caller's own ``sandbox_path`` matches it whether or
    not ``strip_prefix`` succeeded.

    **Trailing component** — a fallback for a location this run did not strip, which still ends
    in the file's name.  It cannot identify anything: a written ``main.bicep`` and an unrelated
    ``/vendor/main.bicep`` match the same key equally well, and picking a longest or first match
    only chooses between guesses.  So it renders :data:`_UNIDENTIFIED` and claims no position.
    The name is still withheld, because a location that ends in a written file's name may *be*
    that file, and that file's name may be content the framework hid.

    Either way the *entire* location is replaced rather than the matched part: half of a path
    that contained the name is still the name, and the directories around it are the sandbox's
    internal layout, which ``format_diagnostics`` does not put in front of the model either.
    """
    if not rename or not location:
        return location
    for candidate in (location, absolute):
        if candidate and candidate in rename:
            return rename[candidate]
    if any(location.endswith("/" + real) for real in rename):
        return _UNIDENTIFIED
    return location


def format_diagnostics(
    diagnostics: list[dict[str, Any]],
    phase: str,
    *,
    strip_prefix: str | None = None,
    rename: Mapping[str, str] | None = None,
) -> str:
    """Render a compact human-readable summary of SARIF diagnostics.

    ``strip_prefix`` removes the per-call sandbox directory from locations and paths in
    messages, keeping diagnostics stable across retries. ``rename`` supplies safe display
    names for both surfaces: pass every written file, including visible names mapped to
    themselves, under its relative and absolute spellings. Exact matches use that display
    name; ambiguous trailing matches withhold the name without attributing a request position.

    Message paths may be quoted or bare, including ``file://`` URIs. Other URI schemes and
    paths outside ``strip_prefix`` with no rename match stay as reported: this is a display
    policy for known paths, not a general redactor of compiler text.
    """
    if not diagnostics:
        return f"{phase}: no diagnostics"
    lines = [f"{phase}: {len(diagnostics)} diagnostic(s)"]
    for d in diagnostics:
        loc_parts: list[str] = []
        for loc in d.get("locations", []):
            raw = loc.get("file", "")
            f = _renamed(_relative_location(raw, strip_prefix), raw.removeprefix("file://"), rename)
            ln = loc.get("line")
            col = loc.get("column")
            if not f:
                continue
            if ln and col:
                loc_parts.append(f"{f}:{ln}:{col}")
            elif ln:
                # Bicep emits `charOffset` rather than `startColumn`, so there is usually no
                # column to show. Printing the line alone beats printing "None".
                loc_parts.append(f"{f}:{ln}")
            else:
                loc_parts.append(f)
        loc = ", ".join(loc_parts) if loc_parts else "—"
        message = _message_text(str(d.get("message", "")), strip_prefix, rename)
        lines.append(f"  [{d.get('level', '?')}] {d.get('rule', '')} @ {loc}: {message}")
    return "\n".join(lines)


def _relative_location(uri: str, strip_prefix: str | None) -> str:
    """Turn a SARIF artifact URI back into the path the caller asked about."""
    if not uri:
        return ""
    path = uri.removeprefix("file://")
    if strip_prefix:
        prefix = strip_prefix.rstrip("/") + "/"
        if not strip_prefix.startswith("/"):
            # A relative call directory identifies its subtree without knowing the backend base.
            _, found, relative = path.rpartition("/" + prefix)
            if found:
                return relative
        path = path.removeprefix(prefix)
    return path


_MESSAGE_WORD = re.compile(r"""[^\s'"`<>()\[\]{},;]+""")
_MESSAGE_PART = re.compile(
    r"""(?P<quote>['"`])(?P<quoted>[^\r\n]*?)(?P=quote)|""" + _MESSAGE_WORD.pattern
)
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[/\\]")


def _message_path(raw: str, strip_prefix: str | None, rename: Mapping[str, str] | None) -> str:
    """Apply the location policy while preserving unrelated prose and non-file URIs."""
    if _URI_SCHEME.match(raw) and not raw.startswith("file://") and not _DRIVE_PATH.match(raw):
        return raw
    absolute = raw.removeprefix("file://")
    path = absolute.replace("\\", "/")
    shown = _renamed(_relative_location(path, strip_prefix), absolute, rename)
    return raw if shown == path else shown


def _message_text(message: str, strip_prefix: str | None, rename: Mapping[str, str] | None) -> str:
    """Rewrite complete path tokens once, so replacements cannot become new rename inputs."""
    if not strip_prefix and not rename:
        return message

    def word(match: re.Match[str]) -> str:
        raw = match.group()
        path = raw.rstrip(".:!?")
        return _message_path(path, strip_prefix, rename) + raw[len(path) :]

    def part(match: re.Match[str]) -> str:
        quote = match.group("quote")
        if quote is None:
            return word(match)
        raw = match.group("quoted")
        if (
            not any(c.isspace() for c in raw)
            or raw.startswith(("/", "\\", "file://"))
            or _DRIVE_PATH.match(raw)
            or (rename and raw in rename)
        ):
            shown = _message_path(raw, strip_prefix, rename)
        else:
            shown = _MESSAGE_WORD.sub(word, raw)
        return quote + shown + quote

    return _MESSAGE_PART.sub(part, message)
