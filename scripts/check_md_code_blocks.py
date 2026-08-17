# Copyright (c) Microsoft. All rights reserved.
#
# Ported from microsoft/agent-framework (MIT) `python/scripts/check_md_code_blocks.py`,
# which is the original implementation this derives from. The MIT license notice
# above is retained from the source, as the license requires for substantial copies
# and derivatives of the software. The behaviour is adapted to this repository:
# the import gate is the six `maf_sandbox*` packages, the output filter keys on
# `maf_sandbox`, the `pygments` colour-printing dependency is dropped, and the
# script exits cleanly rather than raising a `RuntimeError` traceback.

"""Lint the ```python blocks embedded in markdown against the installed packages.

    uv run python scripts/check_md_code_blocks.py README.md \
        'packages/*/README.md' 'samples/**/*.md' RELEASING.md

A README quickstart that drifted from the API it documents — an import renamed
away, an enum member removed — misleads the next reader, and nothing else in the
gate catches it: ruff lints ``.py`` only and cannot resolve `maf_sandbox*` exports
anyway. This extracts each fenced ```` ```python ```` block, writes it to a temp
file, and runs pyright over it with `typeCheckingMode` off and only
`reportMissingImports` and `reportAttributeAccessIssue` raised to error, then keeps
only those two categories from pyright's output. Everything else — top-level
`await`, undefined `router`/`context` — is the expected noise of an illustrative
snippet, so it is dropped on purpose.

A block that imports none of the `maf_sandbox*` packages is skipped outright: it is
a wiring fragment whose undefined names are the host's, not drift against our API.
That keeps the check quiet on the snippets that are prose-with-code while still
catching the one thing that matters, a real export the package no longer provides.

Report-only in CI today (``continue-on-error``); the gate is flipped on once the
check has stayed green across a release or two (#289).
"""

from __future__ import annotations

import argparse
import glob
import re
import subprocess  # nosec
import sys
import tempfile
from pathlib import Path

#: The package roots a checked block must import at least one of. A block that
#: imports none is a wiring fragment (undefined `router`, `context`, …) whose
#: names are the host's, so it is skipped rather than type-checked. Every root
#: shares the `maf_sandbox` prefix, which is what the missing-import filter keys on.
_MODULES = [
    "maf_sandbox",
    "maf_sandbox_acas",
    "maf_sandbox_bicep",
    "maf_sandbox_codeact",
    "maf_sandbox_docker",
    "maf_sandbox_wslc",
]

#: An import of a tracked module, anchored to the start of a line so a comment
#: (`# import maf_sandbox …`) or a string literal does not satisfy the gate. The
#: six roots share the `maf_sandbox` prefix, so one prefix is enough; `from
#: maf_sandbox_docker import …` matches because `from maf_sandbox` is its prefix.
_IMPORT_RE = re.compile(r"(?m)^[ \t]*(?:import|from)[ \t]+maf_sandbox")

#: `typeCheckingMode` off so undefined names and top-level `await` do not fire;
#: only the two rules that read against the installed packages are raised, which
#: is the whole point — an import the package no longer exports, an attribute a
#: class no longer carries.
_PYRIGHT_CFG = (
    '{"include":["."],"typeCheckingMode":"off",'
    '"reportMissingImports":"error","reportAttributeAccessIssue":"error"}'
)


def expand_file_patterns(patterns: list[str], skip_glob: bool = False) -> list[str]:
    """Expand glob patterns to the markdown files they match, sorted and de-duped.

    ``skip_glob`` treats each pattern as a literal path — the shape a change-detected
    caller uses to pass only the files that changed. `glob.glob` still parses `[`,
    `*` and `?` as metacharacters in that mode, so the pattern is escaped first; a
    literal `sample[1].md` then resolves rather than being read as the character
    class `[1]`.
    """
    all_files: list[str] = []
    for pattern in patterns:
        if skip_glob:
            matches = glob.glob(glob.escape(pattern), recursive=False)
        else:
            matches = glob.glob(pattern, recursive=True)
        all_files.extend(matches)
    return sorted(set(all_files))


def extract_python_code_blocks(markdown_file_path: str) -> list[tuple[str, int]]:
    """The ```` ```python ```` fenced blocks in a markdown file, with the line each starts on.

    A block opens on any fence line whose stripped text starts with ```` ```python ````
    (so ```` ```python3 ```` and trailing whitespace also open one) and closes on the
    next ```` ``` ```` of any language. Other fences (```` ```bash ````, plain ```` ``` ````)
    are never opened, so they are passed over silently — a non-python fence only ever
    *closes* a python block, never opens one, so it adds nothing when no block is open.

    A ```` ```python ```` fence that appears while a block is already open closes that
    block and opens a new one (rather than resetting it and losing the in-progress
    content), and a block still open at end-of-file is returned as if closed — a missing
    closing fence is malformed markdown, and dropping its content would silently hide
    whatever drift it carries.
    """
    with open(markdown_file_path, encoding="utf-8") as file:
        lines = file.readlines()

    code_blocks: list[tuple[str, int]] = []
    in_code_block = False
    current_block: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if in_code_block:
            if stripped.startswith("```"):
                in_code_block = False
                code_blocks.append(("\n".join(current_block), i - len(current_block) + 1))
                if stripped.startswith("```python"):
                    in_code_block = True
                    current_block = []
            else:
                current_block.append(line)
        elif stripped.startswith("```python"):
            in_code_block = True
            current_block = []

    if in_code_block:
        code_blocks.append(("\n".join(current_block), len(lines) - len(current_block) + 1))

    return code_blocks


def _imports_a_tracked_module(code_block: str) -> bool:
    """Whether a block imports at least one of the `maf_sandbox*` packages.

    Anchored to the start of a line, so a comment (`# import maf_sandbox …`) or a
    string literal does not satisfy the gate. `from maf_sandbox_docker import …`
    counts because `from maf_sandbox` is its prefix — the six roots share it, and a
    block importing any of them is one whose drift against the package is worth
    catching.
    """
    return _IMPORT_RE.search(code_block) is not None


def _relevant_errors(pyright_stdout: str) -> list[str]:
    """The pyright lines worth acting on: missing imports of our packages, or bad attributes.

    `reportMissingImports` is gated on `maf_sandbox` so a stdlib or guest-module
    miss (none of which reach a checked block, but defensively) does not false-fire.
    `reportAttributeAccessIssue` is kept as-is — on a checked block every attribute
    access lands on one of our types, so the line names the class that drifted.
    """
    return [
        line
        for line in pyright_stdout.splitlines()
        if ("reportMissingImports" in line and "maf_sandbox" in line)
        or "reportAttributeAccessIssue" in line
    ]


def check_code_blocks(
    markdown_file_paths: list[str],
    exclude_patterns: list[str] | None = None,
    repo_root: Path | None = None,
) -> list[str]:
    """Type-check the importing ```python blocks; return the `file:line` of each that drifted.

    A returned list is the drift — empty means clean. Each entry is the markdown
    path and starting line of a block pyright flagged, for the caller to print.
    ``repo_root`` is the cwd pyright runs from (so it resolves the editable
    workspace packages); it defaults to this script's repository root.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    exclude_patterns = exclude_patterns or []
    failures: list[str] = []

    for markdown_file_path in markdown_file_paths:
        # Normalise to forward slashes before the substring match so a Windows
        # backslash path still hits a forward-slash `--exclude` (and vice versa); CI
        # is Linux, but the documented local run is cross-platform.
        posix_path = markdown_file_path.replace("\\", "/")
        if any(pattern.replace("\\", "/") in posix_path for pattern in exclude_patterns):
            print(f"SKIP {markdown_file_path} (matches --exclude)")
            continue
        code_blocks = extract_python_code_blocks(markdown_file_path)
        for code_block, line_no in code_blocks:
            location = f"{markdown_file_path}:{line_no}"
            if not _imports_a_tracked_module(code_block):
                print(f"SKIP {location} (imports no maf_sandbox* module)")
                continue

            with tempfile.TemporaryDirectory() as tmp_dir:
                pyright_cfg = Path(tmp_dir) / "pyrightconfig.json"
                pyright_cfg.write_text(_PYRIGHT_CFG, encoding="utf-8")
                snippet = Path(tmp_dir) / "snippet.py"
                snippet.write_text(code_block, encoding="utf-8")

                result = subprocess.run(  # nosec
                    ["uv", "run", "pyright", "-p", tmp_dir],
                    capture_output=True,
                    text=True,
                    cwd=str(repo_root),
                )
                # Fail-closed: pyright that did not run (a bad install, an internal
                # error) emits nothing on stdout and exits non-zero. Treating that as
                # OK would pass drift silently — the checks-that-cover-nothing
                # pattern — so a tool failure is a FAIL, never a quiet OK. A genuine
                # clean pass is exit 0 with empty stdout; a found-issues pass is exit
                # non-zero *with* stdout, which falls through to the filter below.
                if result.returncode != 0 and not result.stdout.strip():
                    failures.append(location)
                    print(f"FAIL {location} (pyright produced no output; exit {result.returncode})")
                    for line in result.stderr.splitlines():
                        print(f"      {line}")
                    continue
                errors = _relevant_errors(result.stdout)
                if errors:
                    failures.append(location)
                    print(f"FAIL {location}")
                    for error in errors:
                        print(f"      {error}")
                else:
                    print(f"OK   {location}")

    return failures


def main(argv: list[str]) -> int:
    """CLI entry: glob-expand the file arguments, check their ```python blocks, exit 1 on drift."""
    parser = argparse.ArgumentParser(
        description="Lint the ```python blocks in markdown against the installed maf_sandbox* packages.",
    )
    parser.add_argument(
        "markdown_files",
        nargs="+",
        help="Markdown files to check (supports glob patterns, e.g. 'samples/**/*.md').",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude files whose path contains this substring (repeatable).",
    )
    parser.add_argument(
        "--no-glob",
        action="store_true",
        help="Treat file arguments as literal paths rather than glob patterns.",
    )
    args = parser.parse_args(argv[1:])

    files = expand_file_patterns(args.markdown_files, skip_glob=args.no_glob)
    if not files:
        print("no markdown files matched", file=sys.stderr)
        return 2

    failures = check_code_blocks(files, args.exclude)
    if failures:
        print("\nDrift found in:\n" + "\n".join(f"  {f}" for f in failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
