"""Keep CodeQL configurations identical across PR and main analyses."""

from __future__ import annotations

import json
import os
from pathlib import Path

LANGUAGES = ("actions", "javascript-typescript", "python", "rust", "go")


def analysis_matrix(event: str) -> list[dict[str, str]]:
    """Return every configuration GitHub needs to compare PR alerts with main."""
    if event not in {"pull_request", "push", "schedule", "workflow_dispatch"}:
        raise ValueError(f"Unsupported CodeQL event: {event}")
    return [
        {"language": language, "build-mode": "manual" if language == "go" else "none"}
        for language in LANGUAGES
    ]


def main() -> None:
    """Write the analysis matrix and its summary to GitHub Actions."""
    matrix = analysis_matrix(os.environ["GITHUB_EVENT_NAME"])
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"matrix={json.dumps(matrix, separators=(',', ':'))}\n")
    languages = ", ".join(row["language"] for row in matrix)
    summary = f"CodeQL languages: {languages}\n"
    print(summary, end="")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write(summary)


if __name__ == "__main__":
    main()
