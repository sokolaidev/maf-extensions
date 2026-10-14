"""Reject WSLC live reports that skipped the required engine and file contracts."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED = frozenset(
    {
        "test_files_out_shared_conformance",
        "test_files_out_binary_metadata_caps_and_guest_independence",
        "test_write_checks_remove_private_host_copies",
        "test_instance_disposal_conforms_against_engine_inventory[False]",
    }
)


def assess(path: Path) -> list[str]:
    """Require successful concrete cases, even when pytest accepts an all-skipped run."""
    cases = list(ET.parse(path).iter("testcase"))
    passed = {
        case.attrib.get("name")
        for case in cases
        if all(case.find(tag) is None for tag in ("skipped", "failure", "error"))
    }
    failures = [f"required WSLC test did not pass: {name}" for name in sorted(REQUIRED - passed)]
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error")):
        failures.append("WSLC report contains failed tests")
    return failures


def main(argv: list[str]) -> int:
    """CLI entry."""
    if len(argv) != 2:
        print(f"usage: {argv[0]} junit.xml", file=sys.stderr)
        return 2
    failures = assess(Path(argv[1]))
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print("WSLC live report passed the required execution, file and disposal cases")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
