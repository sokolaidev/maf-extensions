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
        _require(type(saved["bytes"]) is int and saved["bytes"] > 0, "No stored XML was read back")
        calls = [record for record in evidence if record["stage"] == "tool_call_ended"]
        _require(len(calls) == attempt + 1, "A draw.io call has no timing record")
        _require(len({call["call"] for call in calls}) == len(calls), "Duplicate call IDs")
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
