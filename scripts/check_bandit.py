"""Run Bandit and refuse findings beyond the recorded adoption baseline."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "bandit-baseline.json"
TARGETS = ("packages", "scripts", "samples", "images", "tests")
FIELDS = ("filename", "test_id", "issue_severity", "issue_confidence", "code")


def finding_key(finding: dict[str, Any]) -> tuple[str, ...]:
    """Match source context and multiplicity, independent of line numbers and path separators."""
    values = {field: str(finding[field]) for field in FIELDS}
    values["filename"] = values["filename"].replace("\\", "/").removeprefix("./")
    values["code"] = "".join(
        line.partition(" ")[2] for line in values["code"].splitlines(keepends=True)
    ).replace("\r\n", "\n")
    return tuple(values[field] for field in FIELDS)


def check_report(report: dict[str, Any], baseline: list[dict[str, Any]]) -> int:
    """Fail on incomplete scans or findings whose exact source context exceeds its allowance."""
    if report["errors"] or not report["metrics"].get("_totals", {}).get("loc", 0):
        print(f"Bandit did not complete a nonempty scan: {report['errors']}", file=sys.stderr)
        return 2
    remaining: Counter[tuple[str, ...]] = Counter()
    for entry in baseline:
        if (
            type(entry["count"]) is not int
            or entry["count"] < 1
            or entry["issue_severity"] != "MEDIUM"
        ):
            raise ValueError("Baseline entries must have positive counts and MEDIUM severity")
        remaining[tuple(entry[field] for field in FIELDS)] += entry["count"]
    new = []
    for finding in report["results"]:
        key = finding_key(finding)
        if remaining[key]:
            remaining[key] -= 1
        else:
            new.append(finding)
    for finding in new:
        print(
            f"{finding['filename']}:{finding['line_number']}: {finding['test_id']} "
            f"{finding['issue_severity']}/{finding['issue_confidence']}: {finding['issue_text']}"
        )
    print(f"Bandit: {len(new)} new findings; {len(report['results']) - len(new)} baseline findings")
    return 1 if new else 0


def scan(report_path: Path) -> int:
    """Retain the unfiltered threshold report even when the gate fails."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "bandit",
            "-c",
            "pyproject.toml",
            "-r",
            *TARGETS,
            "--severity-level",
            "medium",
            "--confidence-level",
            "medium",
            "--format",
            "json",
            "--output",
            str(report_path),
            "--exit-zero",
        ],
        cwd=ROOT,
        check=False,
    )
    if result.returncode:
        return result.returncode
    return check_report(
        json.loads(report_path.read_text(encoding="utf-8")),
        json.loads(BASELINE.read_text(encoding="utf-8")),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the local or CI gate, optionally retaining its complete JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, help="Retain the full Bandit JSON report")
    args = parser.parse_args(argv)
    try:
        if args.report is not None:
            return scan(args.report.resolve())
        with tempfile.TemporaryDirectory(prefix="bandit-") as directory:
            return scan(Path(directory) / "report.json")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Bandit gate could not validate its report or baseline: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
