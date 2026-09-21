"""Verify sample 18's host evidence for XML repair, per-call timings and cleanup."""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from typing import Any

_PREFIX = "  [measured] "


def records(output: str) -> list[dict[str, Any]]:
    """Read only complete JSON records tagged by the sample's host."""
    return [
        json.loads(line.removeprefix(_PREFIX))
        for line in output.splitlines()
        if line.startswith(_PREFIX + "{")
    ]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _outcome(validation: dict[str, Any]) -> str:
    """Read the fixed contract prefix or a released sample's legacy result."""
    diagnostic = validation["diagnostic"]
    lines = diagnostic.split("\n", 2)
    if lines[0] == "The workload ran to a definitive result.":
        if len(lines) == 3 and lines[1] in {"Result: created", "Result: refused"}:
            return lines[1].removeprefix("Result: ")
    elif lines[0] == "The workload did not reach a definitive result.":
        if len(lines) > 1 and not lines[1].startswith("Result:"):
            return "incomplete"
    elif diagnostic.startswith("Error:"):
        return "refused"
    elif re.fullmatch(
        re.escape(f"{validation['call']}/diagram.drawio") + r"(?: \([0-9]+ bytes\))?", diagnostic
    ):
        return "created"
    raise ValueError("Unrecognized converter result")


def assess(output: str) -> list[str]:
    """Return reasons the transcript fails to establish the repair and cleanup contract."""
    try:
        evidence = records(output)
        stages = {record["stage"]: record for record in evidence}
        configuration = stages["configuration"]
        _require(
            configuration["backend"] == "acas"
            and configuration["guest_egress"] == "closed"
            and configuration["allowed_hosts"] == [],
            "ACAS closed egress was not confirmed",
        )
        rejected = stages["rejected"]
        _require(
            rejected["delivered"] == 0
            and "Cell 'api_to_database'.target must reference a vertex" in rejected["diagnostic"],
            "The broken edge was not rejected without delivery",
        )
        saved = stages["saved_and_read"]
        attempt = saved["attempt"]
        _require(type(attempt) is int and 1 <= attempt <= 3, "Invalid repair attempt count")
        expected_stages = [
            "configuration",
            "authored",
            "corrupted",
            "tool_call_ended",
            "validation",
            "rejected",
        ]
        for number in range(1, attempt + 1):
            expected_stages.extend(["repair", "tool_call_ended", "validation"])
            if number < attempt:
                expected_stages.append("repair_rejected")
        expected_stages.extend(["saved_and_read", "storage_cleanup", "sandbox_cleanup", "complete"])
        _require(
            [record["stage"] for record in evidence] == expected_stages,
            "Missing, duplicate or out-of-order repair evidence",
        )
        _require(
            stages["corrupted"]["edge"] == "api_to_database"
            and stages["corrupted"]["target"] == "missing_database",
            "Wrong deliberate corruption",
        )
        validations = [record for record in evidence if record["stage"] == "validation"]
        _require(
            validations[0]["diagnostic"] == rejected["diagnostic"]
            and _outcome(validations[0]) == "refused"
            and validations[0]["delivered"] == 0,
            "Rejection does not match the converter result",
        )
        repairs = [record for record in evidence if record["stage"] == "repair"]
        for number, repair in enumerate(repairs, 1):
            _require(
                type(repair["attempt"]) is int
                and repair["attempt"] == number
                and repair["diagnostic"] == validations[number - 1]["diagnostic"],
                "Repair attempt or prompt diagnostic does not match the previous result",
            )
        for record in [stages["authored"], stages["corrupted"], *repairs]:
            _require(
                re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is not None, "Missing XML hash"
            )
        retries = [record for record in evidence if record["stage"] == "repair_rejected"]
        for number, retry in enumerate(retries, 1):
            _require(
                type(retry["attempt"]) is int
                and retry["attempt"] == number
                and _outcome(validations[number]) in {"refused", "incomplete"}
                and retry["diagnostic"] == validations[number]["diagnostic"],
                "Missing failed repair diagnostic",
            )
        _require(type(saved["bytes"]) is int and saved["bytes"] > 0, "No stored XML was read back")
        calls = [record for record in evidence if record["stage"] == "tool_call_ended"]
        _require(len(calls) == attempt + 1, "A draw.io call has no timing record")
        _require(len({call["call"] for call in calls}) == len(calls), "Duplicate call IDs")
        inputs = [stages["corrupted"], *repairs]
        for index, validation in enumerate(validations):
            _require(
                validation["call"] == calls[index]["call"]
                and validation["sha256"] == inputs[index]["sha256"],
                "Validation result belongs to a different call or XML input",
            )
            success = index == attempt
            _require(
                type(validation["delivered"]) is int
                and validation["delivered"] == int(success)
                and bool(validation["diagnostic"])
                and (_outcome(validation) == "created") is success,
                "Validation outcome does not match artifact delivery",
            )
        for call in calls:
            _require(
                call["tool"] == "create_drawio"
                and call["kind"] == "drawio"
                and re.fullmatch(r"[0-9a-f]{32}", call["call"]) is not None,
                "Invalid draw.io call identity",
            )
            seconds = call["seconds"]
            _require(
                type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0,
                "Invalid call duration",
            )
            _require(call["failure"] is None and call["unclean"] == 0, "Call failed or was unclean")
        _require(saved["path"] == f"{calls[-1]['call']}/diagram.drawio", "Wrong call's artifact")
        storage = stages["storage_cleanup"]
        _require(storage["failures"] == 0 and storage["attempted"] == 1, "Storage cleanup failed")
        _require(stages["sandbox_cleanup"]["complete"] is True, "Sandbox cleanup incomplete")
        _require(evidence[-1]["stage"] == "complete", "Sample did not finish")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return [f"Missing or invalid draw.io evidence: {exc}"]
    return []


def main(argv: list[str]) -> int:
    """Check a saved transcript and print the duration of every draw.io call."""
    if len(argv) != 2:
        print(f"usage: {argv[0]} <sample-output>", file=sys.stderr)
        return 2
    output = Path(argv[1]).read_text(encoding="utf-8")
    failures = assess(output)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    for call in records(output):
        if call["stage"] == "tool_call_ended":
            print(f"create_drawio {call['call']}: {call['seconds']:.3f}s (including cleanup)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
