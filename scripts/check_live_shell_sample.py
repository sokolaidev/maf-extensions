"""Verify Learn grounding, shell validation, cleanup and the retained script's bytes."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from live_sample_evidence import duration, positive_integer, records


def assess(output: str, artifact: bytes) -> list[str]:
    """Bind the clean validator result and read-back to the same artifact."""
    try:
        measured = records(output)
    except (TypeError, ValueError) as exc:
        return [f"Malformed measurement: {exc}"]
    searches = [item for item in measured if item.get("stage") == "learn"]
    calls = [item for item in measured if item.get("stage") == "validation_call"]
    validations = [item for item in measured if item.get("stage") == "validation"]
    cleanup = [item for item in measured if item.get("stage") == "cleanup"]
    artifacts = [item for item in measured if item.get("stage") == "artifact"]
    if not searches or not positive_integer(searches[-1].get("results")):
        return ["No successful Microsoft Learn search was observed"]
    if not calls or any(
        item.get("complete") is not True or not duration(item.get("seconds")) for item in calls
    ):
        return ["Validation or sandbox disposal was incomplete"]
    if len({item.get("call") for item in calls}) != len(calls) or any(
        not item.get("call") for item in calls
    ):
        return ["Validation call identities are missing or duplicated"]
    if (
        len(validations) != len(calls)
        or not validations
        or validations[-1].get("clean") is not True
    ):
        return ["The final script has no clean validation"]
    if cleanup != [{"stage": "cleanup", "complete": True}]:
        return ["Temporary storage or sandbox cleanup was not confirmed"]
    digest = hashlib.sha256(artifact).hexdigest()
    if (
        not artifact
        or len(artifacts) != 1
        or artifacts[0].get("sha256") != digest
        or artifacts[0].get("bytes") != len(artifact)
    ):
        return ["Retained script does not match the measured artifact"]
    if validations[-1].get("sha256") != digest:
        return ["Retained script differs from the clean validated script"]
    return []


def main(argv: list[str]) -> int:
    """Check the retained log and deployment script."""
    if len(argv) != 3:
        print(f"usage: {argv[0]} <log> <script>", file=sys.stderr)
        return 2
    failures = assess(Path(argv[1]).read_text("utf-8"), Path(argv[2]).read_bytes())
    for failure in failures:
        print(failure, file=sys.stderr)
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
