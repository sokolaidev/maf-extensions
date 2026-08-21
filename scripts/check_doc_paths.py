"""Refuse a documentation reference that no longer resolves.

    python scripts/check_doc_paths.py

Two kinds of reference break when a document moves, and until this existed neither was read
by anything. A restructure that moved `docs/design/` to `docs/sandbox/` left eleven behind and
passed every check (#556).

**Relative markdown links** — `](../../docs/sandbox/architecture.md)` — break visibly, on
GitHub, for anyone who clicks.

**Repo-shaped paths written in prose** — a docstring saying "see
``docs/sandbox/research/egress-resolution.md``" — break invisibly, and seven of that eleven
were in `_protocol.py` and `_router.py`, which ship inside the wheel. A reader who follows one
is reading a released package that points at a directory nobody shipped.

Both halves resolve against the working tree and nothing else: no network, no anchor
resolution, no external URL. A dead link on the open internet is a different problem with a
different cadence, and reaching for it here would make the gate slow and flaky.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: Markdown inline links and images, capturing the target. Reference-style definitions
#: (`[label]: target`) are matched separately below; both spellings resolve the same way.
_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)>\s]+)")
_REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)", re.MULTILINE)

#: A path into this repository, written in prose. Anchored on a top-level directory and
#: required to end in an extension, because those two together are what make it a *path* rather
#: than a phrase — `packages/maf-sandbox` is a directory a sentence may name loosely, and
#: `docs/sandbox/architecture.md` is a thing a reader is being told to open.
#:
#: The lookbehind is what keeps a URL out: every `packages/…` inside
#: `https://github.com/…/blob/main/packages/…` is preceded by `/`, and a bare mention never is.
#: The lookahead stops the match before sentence punctuation, so a path ending a sentence is not
#: reported with the full stop attached.
_PROSE_PATH = re.compile(
    r"(?<![\w./-])((?:docs|packages|samples|scripts|tests)/[\w./-]+\.[A-Za-z]{2,4})(?![\w/])"
)

#: Scanned for prose paths: the two surfaces where a dead reference reaches a reader — markdown,
#: which renders on GitHub and PyPI, and shipped source, which goes out in the wheel.
#:
#: `tests/` and `scripts/` are deliberately absent, and for the same reason rather than as an
#: oversight: both *construct* paths as data. A test names paths that must not exist, and
#: `check_live_diagram_sample.py` names `samples/07_docker_diagram/out/diagram.png` — an
#: artefact a run produces, correctly absent from the tree. A checker that reported those would
#: be switched off within the week, and a checker that is off finds nothing at all.
_PROSE_GLOBS = ("*.md", "packages/*/src/**/*.py")

#: A glob is a pattern, not a path; `packages/*/README.md` names five files and resolves to
#: none of them.
_GLOB_CHARACTERS = ("*", "?", "[")


def tracked(repo_root: Path, *globs: str) -> list[Path]:
    """Files git tracks matching ``globs``, so an untracked scratch file is never scanned."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", *globs],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return [repo_root / name for name in listed.stdout.split("\0") if name]


def link_targets(text: str) -> list[str]:
    """Every link target in ``text``, both inline and reference-style."""
    return _INLINE_LINK.findall(text) + _REFERENCE_LINK.findall(text)


def is_local(target: str) -> bool:
    """Whether ``target`` names something in this repository rather than elsewhere.

    A pure fragment (`#section`) points within the page and has no file to check; a scheme —
    `https:`, `mailto:` — points off the tree entirely. Both are skipped rather than resolved.
    """
    if not target or target.startswith("#"):
        return False
    return not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)


def resolve(target: str) -> str:
    """``target`` without the parts that name a place *inside* a document rather than a file."""
    return target.split("#", 1)[0].split("?", 1)[0]


def broken_links(repo_root: Path) -> list[str]:
    """One line per markdown link whose target is not in the working tree."""
    out: list[str] = []
    for path in tracked(repo_root, "*.md"):
        text = path.read_text("utf-8")
        for target in link_targets(text):
            if not is_local(target):
                continue
            bare = resolve(target)
            if not bare or any(character in bare for character in _GLOB_CHARACTERS):
                continue
            if not (path.parent / bare).exists():
                out.append(f"{path.relative_to(repo_root).as_posix()}: link -> {target}")
    return out


def names_something(named: str, repo_root: Path, tracked_paths: set[str]) -> bool:
    """Whether ``named`` points at a file that exists, from the repo root or from a package.

    The suffix half is what keeps this quiet, and it is not laxity. Prose inside a package says
    ``tests/test_acas_e2e.py`` and means that package's tests; prose in a design document says
    ``scripts/import_disk_image.py`` and means whichever package ships it. Resolving those from
    the repository root reports six references that are all perfectly correct.

    It stays strict against the defect it exists for, because a moved document changes a
    **middle** segment: ``docs/design/architecture.md`` is a suffix of nothing once the file
    lives at ``docs/sandbox/architecture.md``.
    """
    if (repo_root / named).exists():
        return True
    return any(candidate.endswith("/" + named) for candidate in tracked_paths)


def broken_prose_paths(repo_root: Path) -> list[str]:
    """One line per repo-shaped path written in prose that names nothing in the tree."""
    tracked_paths = {path.relative_to(repo_root).as_posix() for path in tracked(repo_root)}
    out: list[str] = []
    for path in tracked(repo_root, *_PROSE_GLOBS):
        for match in _PROSE_PATH.finditer(path.read_text("utf-8")):
            named = match.group(1)
            if any(character in named for character in _GLOB_CHARACTERS):
                continue
            if not names_something(named, repo_root, tracked_paths):
                out.append(f"{path.relative_to(repo_root).as_posix()}: names -> {named}")
    return out


def repo_root() -> Path:
    """The tree every reference is resolved against.

    Its own function so a test can point the whole check at a fixture repository without
    patching :mod:`pathlib` out from under everything else that runs in the same process.
    """
    return Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    """CLI entry: report every unresolvable documentation reference, and exit 1 if there is one."""
    if len(argv) != 1:
        print(f"usage: {argv[0]}", file=sys.stderr)
        return 2
    root = repo_root()
    problems = sorted(broken_links(root) + broken_prose_paths(root))
    if not problems:
        print("every documentation link and path resolves")
        return 0
    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        f"\n{len(problems)} reference(s) do not resolve. A moved document leaves these behind "
        "and nothing else reads them — the ones in packages/*/src ship inside the wheel, so a "
        "reader follows them out of a released package.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
