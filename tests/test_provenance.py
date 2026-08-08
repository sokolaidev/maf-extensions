"""The extraction is complete; this is what keeps it that way.

`AGENTS.md` forbids naming or linking the private application these packages were extracted
from. Nothing enforced that, and one reference had already returned by the time anyone
looked — in a build-configuration comment, which is where attention is thinnest.

This is not a security control. Anything already pushed is public and stays public, so the
only moment a slip is still cheap to fix is on the way in, which is the moment this runs.
"""

from __future__ import annotations

import base64
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

OWNER = "sokolaidev"
THIS_REPO = "maf-extensions"

# Base64 so the guard does not itself put the forbidden name into the repository.
HOST_REPO = base64.b64decode(b"YXRzLW1hZg==").decode()

# `[\w-]+` rather than including `.`, so a trailing `.git` or a sentence-ending full stop
# does not become part of the captured name.
REPO_LINK = re.compile(rf"github\.com/{OWNER}/([\w-]+)", re.IGNORECASE)


def names_the_host(line: str) -> bool:
    return HOST_REPO in line.lower()


def foreign_repos(line: str) -> list[str]:
    """Repositories in this account, other than this one, that ``line`` links to."""
    return [name for name in REPO_LINK.findall(line) if name.lower() != THIS_REPO]


def tracked_lines() -> Iterator[tuple[str, int, str]]:
    """Every line git would publish, as ``(path, line number, text)``."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    for name in listing.split("\0"):
        if not name:
            continue
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary, or absent in a partial checkout
        for number, line in enumerate(text.splitlines(), start=1):
            yield name, number, line


class TestTheHostApplicationIsNeverNamed:
    """Its name in a public file points readers at something they cannot open."""

    def test_no_tracked_file_names_it(self):
        hits = [
            f"{name}:{number}"
            for name, number, line in tracked_lines()
            if names_the_host(line)
        ]
        assert not hits, f"the host repository is named at: {', '.join(hits)}"


class TestNoLinksToOtherRepositoriesInThisAccount:
    """A link is the other half of naming it, and can carry no name at all.

    Every repository in this account except this one is presumed private. If a public sibling
    is ever worth linking, add it beside `THIS_REPO` rather than loosening the pattern.
    """

    def test_every_link_to_this_account_points_here(self):
        hits = [
            f"{name}:{number} -> {found}"
            for name, number, line in tracked_lines()
            for found in [foreign_repos(line)]
            if found
        ]
        assert not hits, f"links to other repositories in this account: {hits}"


class TestTheGuardItselfWorks:
    """A pattern that matches nothing passes forever and protects nothing."""

    def test_the_name_is_matched_whatever_its_case(self):
        assert names_the_host(f"the {HOST_REPO.upper()} repository")

    def test_this_repository_is_not_the_forbidden_name(self):
        assert not names_the_host(f"{THIS_REPO} is fine")

    def test_a_link_to_another_repository_is_flagged(self):
        assert foreign_repos(f"https://github.com/{OWNER}/somewhere/issues/1") == [
            "somewhere"
        ]

    def test_a_link_to_this_repository_is_not(self):
        assert foreign_repos(f"https://github.com/{OWNER}/{THIS_REPO}/issues/1") == []
