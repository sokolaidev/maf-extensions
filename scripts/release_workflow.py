"""Portable release-step behavior shared by the workflows and local checks."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path


def _append(variable: str, text: str) -> None:
    with Path(os.environ[variable]).open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def proposal(kind: str, version: str, output: Path) -> int:
    """Write proposal prose without passing it through a shell."""
    template = Path(__file__).with_name("templates") / f"{kind}-body.md"
    body = template.read_text("utf-8").replace("@VERSION@", version)
    body = body.replace("@MINOR@", ".".join(version.split(".")[:2]))
    output.write_text(body, encoding="utf-8", newline="\n")
    return 0


def notes(package: str, version: str) -> int:
    """Append the exact version's changelog section to the Actions output file."""
    changelog = Path("packages") / package / "CHANGELOG.md"
    section: list[str] = []
    capture = False
    for line in changelog.read_text("utf-8").splitlines():
        if (
            line.startswith(f"## [{version}]")
            or line.startswith(f"## {version} ")
            or line == f"## {version}"
        ):
            capture = True
            continue
        if capture and line.startswith("## "):
            break
        if capture:
            section.append(line)
    body = "\n".join(section).rstrip()
    if not body.strip():
        print(
            f"::error::no '## [{version}]' or '## {version}' section in "
            f"{changelog.as_posix()} — add the entry before tagging"
        )
        return 1
    # Release prose can itself contain an Actions output delimiter.
    delimiter = f"CHANGELOG_{uuid.uuid4().hex}"
    _append(
        "GITHUB_OUTPUT",
        f"body<<{delimiter}\n{body}\nInstall: `pip install {package}=={version}`\n"
        f"PyPI: https://pypi.org/project/{package}/{version}/\n{delimiter}\n",
    )
    return 0


def gate(mode: str, package: str, version: str, command: list[str]) -> int:
    """Run a checker and report its verdict at the appropriate release phase."""
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as error:
        result = subprocess.CompletedProcess(
            command, 127 if isinstance(error, FileNotFoundError) else 126, "", str(error) + "\n"
        )
    status = result.returncode if result.returncode >= 0 else 128 - result.returncode
    output, errors = result.stdout, result.stderr
    if mode == "breaking":
        sys.stderr.write(errors)
        verdict = status == 0 and output.strip() == "breaking=true"
        if status:
            print(
                f"::warning::could not read whether {package} {version} declares breaking "
                "changes; the dispatch is unaffected"
            )
        _append("GITHUB_OUTPUT", f"breaking={str(verdict).lower()}\n")
        if verdict:
            _append(
                "GITHUB_STEP_SUMMARY",
                "### This release declares breaking changes\n\n"
                f"`{package}` {version} declares breaking changes in its changelog. "
                "Whether that strands the published dependents is a separate question the "
                "work check in this job answers; if they still import, the live check runs "
                "all the same. See [#337](https://github.com/sokolaidev/maf-extensions/issues/337).\n",
            )
        return 0

    sys.stdout.write(output)
    if mode != "dispatch" or status:
        sys.stderr.write(errors)
    if status or mode == "pre-upload":
        return status
    verdict = next((line for line in output.splitlines() if line.startswith("live_check=")), "")
    if verdict not in ("live_check=run", "live_check=skip"):
        label = "post-upload dispatch check" if mode == "dispatch" else "work check"
        print(f"::error::{label} printed no live_check verdict", file=sys.stderr)
        return 1
    if mode == "dispatch":
        _append("GITHUB_OUTPUT", verdict + "\n")
        if errors:
            for line in errors.splitlines():
                if line:
                    print(f"::error::{line}")
            _append(
                "GITHUB_STEP_SUMMARY",
                "### A dependent broke after the upload\n\n"
                f"A published dependent that admitted `{package}` {version} during the upload "
                "window breaks at import time. The upload is immutable, so the release ships "
                "and the live check is dispatched rather than the release refused. The break "
                "needs a follow-up release that keeps the dependent importing. "
                "See [#443](https://github.com/sokolaidev/maf-extensions/issues/443).\n\n"
                f"<details><summary>The import failures</summary>\n\n```\n{errors.rstrip()}\n"
                "```\n</details>\n",
            )
    if verdict == "live_check=skip":
        if mode == "build":
            text = (
                f"### No published dependent admits `{package}` {version} yet (build-time reading)\n\n"
                "The post-upload dispatch job decides whether the live check runs, because a "
                "dependent can admit in the approval window or during the upload. If one does, "
                "the check runs; otherwise it is skipped, and the dependents' own publishes "
                "dispatch it. See [#273](https://github.com/sokolaidev/maf-extensions/issues/273), "
                "[#337](https://github.com/sokolaidev/maf-extensions/issues/337) and "
                "[#443](https://github.com/sokolaidev/maf-extensions/issues/443).\n"
            )
        else:
            text = (
                "### The live check is being skipped\n\n"
                f"No published dependent admits `{package}` {version} after the upload, so a "
                "live run would go red for the ordering of the release train rather than for "
                "the code. The dependents' own publishes dispatch it, which is when its answer "
                "starts meaning something. "
                "See [#273](https://github.com/sokolaidev/maf-extensions/issues/273).\n"
            )
        _append("GITHUB_STEP_SUMMARY", text)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Execute a release step without performing a publish or dispatch itself."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    body = commands.add_parser("proposal")
    body.add_argument("kind", choices=("range", "samples"))
    body.add_argument("version")
    body.add_argument("output", type=Path)
    extract = commands.add_parser("notes")
    extract.add_argument("package")
    extract.add_argument("version")
    for mode in ("breaking", "build", "pre-upload", "dispatch"):
        check = commands.add_parser(mode)
        check.add_argument("package")
        check.add_argument("version")
        check.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.mode == "proposal":
        return proposal(args.kind, args.version, args.output)
    if args.mode == "notes":
        return notes(args.package, args.version)
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a checker command is required after --")
    return gate(args.mode, args.package, args.version, command)


if __name__ == "__main__":
    raise SystemExit(main())
