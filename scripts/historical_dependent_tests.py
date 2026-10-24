"""Apply reviewed, hash-bound corrections to temporary release-test copies."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree

_RECORDS = Path(__file__).with_name("historical_dependent_tests.json")


class _JUnitTreeBuilder(ElementTree.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ValueError("JUnit reports must not contain a DTD")


def prepare_tests(distribution: str, version: str, tests: Path) -> tuple[str, ...]:
    """Refuse changed historical sources; return the explicitly expected failure names."""
    records = json.loads(_RECORDS.read_text(encoding="utf-8"))
    record = records.get(f"{distribution}=={version}")
    if record is None:
        return ()
    for correction in record["files"]:
        path = tests / correction["name"]
        original = path.read_bytes()
        if hashlib.sha256(original).hexdigest() != correction["sha256"]:
            raise ValueError(f"historical test hash mismatch: {distribution} {version}/{path.name}")
        source = original.decode("utf-8")
        for before, after in correction["replacements"]:
            if source.count(before) != 1:
                raise ValueError(f"historical test correction is ambiguous: {path.name}")
            source = source.replace(before, after, 1)
        path.write_bytes(source.encode("utf-8"))
    print(f"Historical test correction: {distribution} {version}: {record['reason']}", flush=True)
    return tuple(record.get("expected_failures", ()))


def verify_expected_failures(report: Path, expected: tuple[str, ...]) -> str | None:
    """Require each recorded defect to execute as an expected failure, never skip or disappear."""
    try:
        # The target rejects DTDs before entity expansion.
        parser = ElementTree.XMLParser(target=_JUnitTreeBuilder())  # nosec B314
        parser.feed(report.read_bytes())
        cases = parser.close().iter("testcase")
        observed = [
            f"{case.get('classname', '').split('.')[-1]}::{case.get('name', '')}"
            for case in cases
            if any(child.get("type") == "pytest.xfail" for child in case.findall("skipped"))
        ]
    except (OSError, ValueError, ElementTree.ParseError) as error:
        return f"cannot verify historical expected failures: {error}"
    if sorted(observed) != sorted(expected):
        return (
            f"historical expected failures changed: expected {list(expected)}, observed {observed}"
        )
    return None
