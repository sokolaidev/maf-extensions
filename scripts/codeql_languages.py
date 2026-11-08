"""Select CodeQL languages from PR inputs; other events analyze every language."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from codeql_go import CONTEXTS

ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = ("actions", "javascript-typescript", "python", "rust", "go")
SHARED = {
    ".github/workflows/codeql.yml",
    "scripts/codeql_languages.py",
    "tests/test_codeql_languages.py",
}
SUFFIXES = {
    "javascript-typescript": {
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".vue",
        ".svelte",
        ".html",
        ".htm",
        ".hbs",
        ".ejs",
        ".es",
        ".es6",
    },
    "python": {".py", ".pyi", ".pyw", ".py2", ".py3"},
    "rust": {".rs"},
    "go": {".go"},
}
MANIFESTS = {
    "javascript-typescript": (
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "bun.lock",
        "bun.lockb",
        "tsconfig*.json",
        "jsconfig*.json",
        ".npmrc",
        ".yarnrc*",
    ),
    "python": (
        "pyproject.toml",
        "uv.lock",
        "uv.toml",
        "setup.cfg",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "pdm.lock",
        "requirements*.txt",
        "requirements*.in",
    ),
    "rust": ("Cargo.toml", "Cargo.toml.template", "Cargo.lock", "rust-toolchain*"),
    "go": ("go.mod", "go.sum", "go.work", "go.work.sum"),
}


def select_languages(paths: Iterable[str]) -> list[str]:
    """Include sources and the build/configuration inputs that affect their analysis."""
    selected: set[str] = set()
    for name in paths:
        path = PurePosixPath(name)
        if name in SHARED or name.startswith(".github/codeql/"):
            return list(LANGUAGES)
        if (name.startswith(".github/workflows/") and path.suffix in {".yml", ".yaml"}) or (
            path.name in {"action.yml", "action.yaml"}
        ):
            selected.add("actions")
        for language, suffixes in SUFFIXES.items():
            if path.suffix in suffixes or any(
                fnmatchcase(path.name, pattern) for pattern in MANIFESTS[language]
            ):
                selected.add(language)
        if name in {"scripts/codeql_go.py", "tests/test_codeql_go.py"} or any(
            name.startswith(context.as_posix() + "/") for context in CONTEXTS
        ):
            selected.add("go")
        if ".cargo" in path.parts or (
            name.startswith("scripts/experiments/mxc_") and path.suffix == ".patch"
        ):
            selected.add("rust")
    return [language for language in LANGUAGES if language in selected]


def changed_paths(root: Path, base: str, head: str) -> list[str]:
    """Read the complete PR diff, preserving deleted paths and both sides of renames."""
    if not all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (base, head)):
        raise ValueError("PR selection requires full base and head commit SHAs")
    result = subprocess.run(
        ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}", "--"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [os.fsdecode(name) for name in result.stdout.split(b"\0") if name]


def analysis_matrix(event: str, root: Path, base: str = "", head: str = "") -> list[dict[str, str]]:
    """Return the affected PR matrix or the full matrix for a main, scheduled or manual scan."""
    if event == "pull_request":
        languages = select_languages(changed_paths(root, base, head))
    elif event in {"push", "schedule", "workflow_dispatch"}:
        languages = list(LANGUAGES)
    else:
        raise ValueError(f"Unsupported CodeQL event: {event}")
    return [
        {"language": language, "build-mode": "manual" if language == "go" else "none"}
        for language in languages
    ]


def main() -> None:
    """Write the selected matrix and a readable explanation to GitHub Actions."""
    matrix = analysis_matrix(
        os.environ["GITHUB_EVENT_NAME"],
        ROOT,
        os.environ.get("BASE_SHA", ""),
        os.environ.get("HEAD_SHA", ""),
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"matrix={json.dumps(matrix, separators=(',', ':'))}\n")
    languages = ", ".join(row["language"] for row in matrix) or "none (no relevant inputs changed)"
    summary = f"CodeQL languages: {languages}\n"
    print(summary, end="")
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
        output.write(summary)


if __name__ == "__main__":
    main()
