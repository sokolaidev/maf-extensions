"""Say whether `uv.lock` still holds the newest agent-framework release the ranges admit.

    python scripts/check_locked_framework.py --packages
    python scripts/check_locked_framework.py <committed lock> <re-resolved lock>

`--packages` prints the distributions this repository has to keep current, one per line, so
`.github/workflows/lock-drift.yml` builds its `uv lock --upgrade-package` arguments from the
same list this reports against. Given two lockfiles it prints a run summary and exits 1 if the
re-resolve moved any of them.

**`uv` decides what the newest admitted release is, not this.** The workflow re-resolves and
this compares the two lockfiles; deriving the range against the index here would be a second
resolver, free to disagree with the real one over a yanked release, a `requires-python` bump
or a marker. A check that disagrees with the resolver is worse than no check.

The exposure it measures is the gap between two versions: the offline suite runs under
`uv sync --locked`, so every behavioural assertion it makes about the framework is made
against the locked version, while a host installing these wheels resolves the newest the
declared range admits (#809).

Nothing here reaches the network, which is what `tests/test_check_locked_framework.py` holds
it to.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

#: The distributions whose locked version has to keep up with what an adopter resolves. The
#: dev group's `ruff`, `pyright` and `pytest` bands are deliberately not here: those are pinned
#: so a new lint minor cannot drift findings across unrelated pull requests, and refreshing
#: them is a decision rather than maintenance.
FRAMEWORK = ("agent-framework-core", "agent-framework-openai")

#: What a distribution the committed lock never recorded is reported as. It is not drift the
#: refresh command fixes, so it is named rather than rendered as a version.
ABSENT = "absent"


def locked_versions(text: str) -> dict[str, str]:
    """The version a `uv.lock` document records for each distribution in `FRAMEWORK`."""
    document = tomllib.loads(text)
    return {
        entry["name"]: entry["version"]
        for entry in document.get("package", [])
        if entry.get("name") in FRAMEWORK
    }


def drift(committed: str, resolved: str) -> list[tuple[str, str, str]]:
    """Each framework distribution the re-resolve moved, as (name, locked, admitted)."""
    was, now = locked_versions(committed), locked_versions(resolved)
    return [
        (name, was.get(name, ABSENT), now[name])
        for name in sorted(now)
        if was.get(name, ABSENT) != now[name]
    ]


def report(moved: list[tuple[str, str, str]]) -> str:
    """The run summary, whichever way it went."""
    if not moved:
        return "`uv.lock` holds the newest agent-framework release the declared ranges admit."
    rows = "\n".join(f"| `{name}` | {was} | {now} |" for name, was, now in moved)
    upgrades = " ".join(f"--upgrade-package {name}" for name, _, _ in moved)
    return (
        "**`uv.lock` is behind what a host installing these wheels resolves.** The offline "
        "suite runs under `uv sync --locked`, so every behavioural assertion it makes about "
        "the framework is made against the locked column below.\n"
        "\n"
        "| Distribution | Locked | Newest the ranges admit |\n"
        "| --- | --- | --- |\n"
        f"{rows}\n"
        "\n"
        "The ranges themselves are not moving — refresh the lock, as a `chore:` pull request "
        "that releases nothing:\n"
        "\n"
        f"```bash\nuv lock {upgrades}\n```\n"
    )


def annotation(moved: list[tuple[str, str, str]]) -> str:
    """One line for the checks page, naming the drift rather than only its existence."""
    listed = ", ".join(f"{name} {was} -> {now}" for name, was, now in moved)
    return (
        f"::error::uv.lock is behind the newest agent-framework the declared ranges admit: "
        f"{listed}. The offline suite tests the locked version; a host installing these wheels "
        f"gets the other one. The run summary carries the command that refreshes it."
    )


def main(argv: list[str]) -> int:
    """Print the distributions, or compare two lockfiles and report what moved."""
    if argv[1:] == ["--packages"]:
        print("\n".join(FRAMEWORK))
        return 0
    if len(argv[1:]) != 2:
        print(
            f"usage: {argv[0]} --packages\n       {argv[0]} <committed lock> <re-resolved lock>",
            file=sys.stderr,
        )
        return 2
    committed, resolved = (Path(name).read_text(encoding="utf-8") for name in argv[1:])
    moved = drift(committed, resolved)
    print(report(moved))
    if not moved:
        return 0
    print(annotation(moved), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
