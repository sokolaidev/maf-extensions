"""Verify live provider requests and an actual compaction reduction from host records."""

from __future__ import annotations

import sys
from pathlib import Path

from live_sample_evidence import duration, positive_integer, records


def assess(output: str) -> list[str]:
    """Reject missing, failed, duplicate or non-compacting benchmark evidence."""
    try:
        measured = records(output)
    except (ValueError, TypeError) as exc:
        return [f"Malformed measurement: {exc}"]
    turns = [item for item in measured if item.get("stage") == "turn"]
    complete = [item for item in measured if item.get("stage") == "complete"]
    if complete != [{"stage": "complete", "turns": 12}] or len(turns) != 12:
        return ["Expected one completion and exactly twelve model request records"]
    selected = {
        name: [item for item in turns if item.get("strategy") == name]
        for name in ("none", "anchored")
    }
    for name, items in selected.items():
        if [item.get("turn") for item in items] != list(range(1, 7)):
            return [f"Expected six ordered, unique turns for {name}"]
        for item in items:
            if (
                item.get("error") is not None
                or not positive_integer(item.get("input_tokens"))
                or not positive_integer(item.get("sent_tokens_local"))
                or not duration(item.get("latency_ms"))
            ):
                return [f"Missing usage, failed request or invalid timing for {name}"]
    if selected["anchored"][-1]["sent_tokens_local"] >= selected["none"][-1]["sent_tokens_local"]:
        return ["Compaction did not reduce the final local prompt projection"]
    return []


def main(argv: list[str]) -> int:
    """Check one retained sample log."""
    if len(argv) != 2:
        print(f"usage: {argv[0]} <log>", file=sys.stderr)
        return 2
    failures = assess(Path(argv[1]).read_text("utf-8"))
    for failure in failures:
        print(failure, file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
