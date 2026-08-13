"""Say whether a release was breaking, by reading the section release-please wrote.

    python scripts/check_release_is_breaking.py <package> <version>

Prints `breaking=true` or `breaking=false` on stdout. The answer is whether that version's
section of `packages/<package>/CHANGELOG.md` carries a `### ⚠ BREAKING CHANGES` heading, which
release-please writes for a `feat!:` subject or a `BREAKING CHANGE:` footer and for nothing
else.

The version number cannot answer this below 1.0.0: `bump-minor-pre-major` makes a breaking
change a minor exactly like a `feat:`, so `0.10.0 -> 0.11.0` looks the same either way. The
changelog is where the distinction survives.

A question it cannot answer is an error, never a `false`. No changelog for that package, or no
section for that version, exits 1 with the reason. A caller acts on the *true* answer — a
breaking core release strands the published dependents until they ship again — so answering
"not breaking" when the file did not say so is the one outcome that would make this worthless.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Matched leniently on the wording alone. The decoration is release-please configuration and
#: the words are not, so a heading whose ⚠ was dropped still counts: reading it as "not
#: breaking" is the wrong way to be wrong.
_BREAKING = re.compile(r"^#{3,} .*\bBREAKING CHANGES?\b", re.MULTILINE)
_NEXT_VERSION = re.compile(r"^## ", re.MULTILINE)


def changelog_path(repo_root: Path, package: str) -> Path:
    """Where a package directory's changelog lives, whether or not it exists."""
    return repo_root / "packages" / package / "CHANGELOG.md"


def section(text: str, release: str) -> str | None:
    """A changelog from just below its `## [<release>]` heading to the next `##`, or None.

    The bound matters more than the find: the sections are adjacent, so a slice that ran on
    would read the next release's headings and call this one breaking.
    """
    heading = re.search(rf"^## \[{re.escape(release)}\]", text, re.MULTILINE)
    if heading is None:
        return None
    body = text[heading.end() :]
    following = _NEXT_VERSION.search(body)
    return body[: following.start()] if following else body


def is_breaking(body: str) -> bool:
    """Whether a section declares breaking changes under a heading of its own."""
    return _BREAKING.search(body) is not None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <package> <version>", file=sys.stderr)
        return 2
    package, release = argv[1], argv[2]
    path = changelog_path(Path(__file__).resolve().parent.parent, package)
    if not path.is_file():
        print(
            f"no changelog at {path.as_posix()}, so nothing here can say whether "
            f"{package} {release} was breaking",
            file=sys.stderr,
        )
        return 1
    body = section(path.read_text("utf-8"), release)
    if body is None:
        print(
            f"{path.as_posix()} has no section for {release}, so nothing here can say "
            f"whether it was breaking",
            file=sys.stderr,
        )
        return 1
    print(f"breaking={'true' if is_breaking(body) else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
