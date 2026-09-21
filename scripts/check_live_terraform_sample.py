"""Verify engine diagnostics and per-call disposal in sample 20's host evidence."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

_CLOSE = re.compile(r"^  \[measured\] validation results: (\d+)$", re.MULTILINE)
_CALL = re.compile(r"^  \[measured\] Call: (.+)$", re.MULTILINE)
_PURGE = re.compile(r"^  \[measured\] Disposed (\d+) sandbox\(es\)\.$", re.MULTILINE)


def assess(output: str, *, engine: str, version: str, backend: str) -> list[str]:
    """Reject missing, contradictory or model-authored validation and cleanup evidence."""
    output = output.replace("\r\n", "\n")
    failures: list[str] = []
    tool = f"{engine}_validate"
    closes = list(_CLOSE.finditer(output))
    count = 0
    if len(closes) != 1:
        failures.append("expected one measured validation block")
    else:
        close = closes[0]
        count = int(close[1])
        heading = f"== Diagnostics as {tool} returned them =="
        start = output.rfind(heading, 0, close.start())
        block = output[start + len(heading) : close.start()] if start >= 0 else ""
        verdicts = re.findall(
            r"^  (?:terraform|opentofu) .*: validation .*\.$", block, re.MULTILINE
        )
        expected = f"  {engine} {version}: validation FAIL (1 errors, 0 warnings); formatting PASS."
        if count < 1 or verdicts != [expected] * count:
            failures.append(f"expected {engine} {version}: validation FAIL in every tool result")
        diagnostics = []
        for line in block.splitlines():
            if line.startswith("  {"):
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and item.get("type") == "terraform_diagnostics":
                        continue
                    diagnostics.append(item)
                except ValueError:
                    failures.append("invalid tool diagnostic")
        if (
            len(diagnostics) != count
            or not diagnostics
            or not all(
                isinstance(item, dict)
                and item.get("severity") == "error"
                and item.get("summary") == "Missing required argument"
                and isinstance(item.get("detail"), str)
                and '"length"' in item.get("detail", "")
                for item in diagnostics
            )
        ):
            failures.append("missing the random provider's required length diagnostic")

    calls: set[str] = set()
    records = _CALL.findall(output)
    if len(records) != count or not records:
        failures.append("validation results and measured calls must have the same positive count")
    for raw in records:
        try:
            record = json.loads(raw)
        except ValueError:
            record = None
        if not isinstance(record, dict):
            failures.append("invalid measured call record")
            continue
        call = record.get("call")
        if not isinstance(call, str) or not call or call in calls:
            failures.append("missing or duplicate call identity")
        else:
            calls.add(call)
        if record.get("tool") != tool or record.get("backend") != backend:
            failures.append("call used the wrong tool or backend")
        if (
            record.get("disposed") is not True
            or record.get("failure", "missing") is not None
            or record.get("unclean") != 0
        ):
            failures.append("a call did not complete with its sandbox disposed")
        seconds = record.get("seconds")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            failures.append("invalid measured call duration")
    purges = list(_PURGE.finditer(output))
    if len(purges) != 1 or (closes and purges[0].start() < closes[-1].end()):
        failures.append("missing final scope purge")
    if re.search(r"^  \[measured\] Not fully disposed:", output, re.MULTILINE):
        failures.append("scope purge left sandboxes undisposed")
    return failures


def main(argv: list[str] | None = None) -> int:
    """Check a saved log against the job's expected backend, engine and pinned version."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--engine", choices=("terraform", "opentofu"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--backend", choices=("docker", "acas"), required=True)
    args = parser.parse_args(argv)
    failures = assess(
        args.output.read_text("utf-8"),
        engine=args.engine,
        version=args.version,
        backend=args.backend,
    )
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if not failures:
        print(
            f"OK  {args.engine} {args.version} on {args.backend}: provider validation and per-call disposal verified"
        )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
