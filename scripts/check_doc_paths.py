"""Refuse a documentation reference that does not resolve.

    python scripts/check_doc_paths.py

Reads every tracked markdown file, and every shipped source file under ``packages/*/src``, and
reports three kinds of reference that name something absent: a relative link, the heading
fragment on one, and a repository path written in prose.

**Resolution is against the tracked tree, never the filesystem.** An untracked or ignored file
satisfies nothing, because a reference is only worth anything to a reader who cloned the
repository — and a build artefact sitting in a local checkout would otherwise make a dead link
pass. A link may also name a *directory*, which git does not track directly; one holding a
tracked file counts.

Out of scope, deliberately: external URLs, which would put the network in the gate, and the
heading of a fragment on anything but a markdown file, which has none to check.
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
#: The extension is any length: capping it hid every reference to a `.bicep`, a `.yaml` or a
#: `.toml`, so the paths most likely to move were the ones this could not see.  It must start
#: with a letter, which is what keeps a version out — `maf-sandbox/0.19.0` is not a file.
#:
#: The lookbehind keeps a URL out: every `packages/…` inside
#: `https://github.com/…/blob/main/packages/…` is preceded by `/`, and a bare mention never is.
_PROSE_PATH = re.compile(
    r"(?<![\w./-])((?:docs|packages|samples|scripts|tests)/[\w./-]+\.[A-Za-z][A-Za-z0-9]*)"
    r"(?![\w/])"
)

#: Scanned for prose paths: the two surfaces where a dead reference reaches a reader — markdown,
#: which renders on GitHub and PyPI, and shipped source, which goes out in the wheel.
#:
#: `tests/` and `scripts/` are absent for the same reason rather than as an oversight: both
#: *construct* paths as data, including ones that are correctly absent, and a checker that
#: reported those would be switched off within the week.
_PROSE_GLOBS = ("*.md", "packages/*/src/**/*.py")

#: A glob is a pattern, not a path; `packages/*/README.md` names five files and resolves to
#: none of them.
_GLOB_CHARACTERS = ("*", "?", "[")

#: A fenced block, opened and closed by the same run of backticks or tildes. Its contents are
#: not markdown, so an ATX-looking line inside one is a comment in some other language.
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")

#: An ATX heading, with any closing `##` run discarded.
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*$", re.MULTILINE)

#: An explicit anchor a document plants for itself, which no heading slug would produce.
_HTML_ANCHOR = re.compile(r"""<a\s[^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

#: Inline markup that is *not* part of a heading's text: code ticks, emphasis runs, and the
#: bracket-and-target of a link, whose visible half survives. Underscore is deliberately absent,
#: though it is an emphasis marker too: it is also a word character GitHub keeps, and the
#: headings here are full of identifiers.
_LINK_TEXT = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKUP = re.compile(r"[`*~]")

#: `#L42` and `#L42-L60` on a source file are GitHub line references, not headings.
_LINE_REFERENCE = re.compile(r"^L\d+(?:-L\d+)?$")


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


def tracked_tree(repo_root: Path) -> tuple[set[str], set[str]]:
    """Every tracked file, and every directory holding one, as repo-relative POSIX paths.

    Directories are derived rather than listed because git tracks no directory of its own, and
    a link to one is ordinary — ``README.md`` alone points at seven.
    """
    files = {path.relative_to(repo_root).as_posix() for path in tracked(repo_root)}
    directories: set[str] = set()
    for name in files:
        parts = name.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            directories.add("/".join(parts[:depth]))
    return files, directories


def link_targets(text: str) -> list[str]:
    """Every link target in ``text``, both inline and reference-style."""
    return _INLINE_LINK.findall(text) + _REFERENCE_LINK.findall(text)


def is_local(target: str) -> bool:
    """Whether ``target`` names something in this repository rather than elsewhere.

    A scheme — `https:`, `mailto:` — points off the tree entirely and is skipped rather than
    resolved. So is the protocol-relative form, `//example.invalid/docs`, which carries no
    scheme to match on and is no more ours for it.
    """
    if not target or target.startswith("//"):
        return False
    return not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target)


def resolve(target: str) -> str:
    """``target`` without the parts that name a place *inside* a document rather than a file."""
    return target.split("#", 1)[0].split("?", 1)[0]


def without_fenced_blocks(text: str) -> str:
    """``text`` with every fenced block blanked, its line count preserved.

    A fence holds some other language, and a `#` starting a line in one is a comment there.
    Reading them as headings invents anchors no rendered page has, so a link to a phantom
    passes: `docs/sandbox/guest-platform-and-commands.md` alone contributes two.
    """
    kept: list[str] = []
    opener: str | None = None
    for line in text.splitlines():
        found = _FENCE.match(line)
        run = found.group(1) if found else ""
        if opener is None:
            if found:
                opener = run
                kept.append("")
                continue
            kept.append(line)
            continue
        kept.append("")
        # CommonMark: a closer is the same character and at least as long as the opener.
        # Reducing both to three let a ``` line close a ```` block, exposing everything after
        # it — which is how a nested markdown sample leaks its own headings.
        if found and run[0] == opener[0] and len(run) >= len(opener):
            opener = None
    return "\n".join(kept)


def slugify(heading: str) -> str:
    """The fragment GitHub derives from a heading's text.

    Lowercase, strip everything that is not a word character, whitespace or a hyphen, then turn
    each remaining whitespace character into a hyphen — *each*, not each run, which is why
    ``## A — B`` becomes ``a--b`` and matching it any other way reports a working link.
    """
    text = _LINK_TEXT.sub(r"\1", heading)
    text = _MARKUP.sub("", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"\s", "-", text)


def anchors(text: str) -> set[str]:
    """Every fragment ``text`` can be linked to: its heading slugs and its explicit anchors.

    Repeated headings are numbered the way GitHub numbers them — the second ``## Notes`` is
    ``#notes-1`` — because a document with two sections of the same name is exactly where a
    reader needs the link to be right.
    """
    prose = without_fenced_blocks(text)
    found: set[str] = set()
    for _level, heading in _HEADING.findall(prose):
        slug = slugify(heading)
        if not slug:
            continue
        # The next *free* slug, not a per-base counter. With `## Notes` twice and a literal
        # `## Notes-1`, a counter yields two anchors where GitHub has three — and the link to
        # the third is then reported broken, which is the direction that gets a check disabled.
        candidate, suffix = slug, 0
        while candidate in found:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        found.add(candidate)
    found.update(_HTML_ANCHOR.findall(prose))
    return found


def _repo_relative(destination: Path, repo_root: Path) -> str | None:
    """``destination`` as a repo-relative POSIX path, or None when it leaves the tree."""
    try:
        return destination.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def _fragment(target: str) -> str:
    """The part after ``#``, which names a place inside a document rather than a document."""
    return target.split("#", 1)[1] if "#" in target else ""


def broken_links(repo_root: Path) -> list[str]:
    """One line per markdown link whose file, or whose heading, is not in the tracked tree."""
    files, directories = tracked_tree(repo_root)
    out: list[str] = []
    for path in tracked(repo_root, "*.md"):
        # Fences are stripped for links exactly as they are for headings: a link inside a
        # markdown sample is text about a link, and gating on it reds a document for showing
        # its reader what one looks like.
        text = without_fenced_blocks(path.read_text("utf-8"))
        here = path.relative_to(repo_root).as_posix()
        for target in link_targets(text):
            if not is_local(target) and not target.startswith("#"):
                continue
            bare = resolve(target)
            if any(character in bare for character in _GLOB_CHARACTERS):
                continue
            # An empty file part means the fragment points within this very page.
            destination = (path.parent / bare) if bare else path
            if bare:
                relative = _repo_relative(destination, repo_root)
                if relative is None or (relative not in files and relative not in directories):
                    out.append(f"{here}: link -> {target}")
                    continue
            fragment = _fragment(target)
            if not fragment or destination.suffix.lower() != ".md":
                continue
            # A source file was already skipped by the line above, so the only `#L42` left is
            # one on a *markdown* target — where the rendered page has no such anchor and the
            # fragment is dead. The exception is the plain view, which does: a query string is
            # what asks for it, and `resolve()` has already dropped it from `bare`.
            if _LINE_REFERENCE.match(fragment) and "?" in target:
                continue
            if fragment not in anchors(destination.read_text("utf-8")):
                out.append(f"{here}: heading -> {target}")
    return out


def package_of(relative_path: str) -> str | None:
    """The package a repo-relative path sits in, or None when it sits outside every one."""
    parts = relative_path.split("/")
    return f"packages/{parts[1]}" if len(parts) >= 2 and parts[0] == "packages" else None


def names_something(named: str, referrer: str, files: set[str]) -> bool:
    """Whether ``named``, written in ``referrer``, points at a tracked file.

    Prose inside a package saying ``tests/test_x.py`` means *that package's* tests, so it is
    resolved against the package and nowhere else — searching the whole repository would let
    one package's reference be satisfied by another's file, which is the opposite of what the
    words say.

    A document outside every package has no such home, so ``scripts/import_disk_image.py`` in a
    design document means whichever package ships it, and only there is a repository-wide
    search the right reading.
    """
    if named in files:
        return True
    package = package_of(referrer)
    if package is not None:
        return f"{package}/{named}" in files
    return any(candidate.endswith("/" + named) for candidate in files)


def broken_prose_paths(repo_root: Path) -> list[str]:
    """One line per repo-shaped path written in prose that names nothing tracked."""
    files, _directories = tracked_tree(repo_root)
    out: list[str] = []
    for path in tracked(repo_root, *_PROSE_GLOBS):
        here = path.relative_to(repo_root).as_posix()
        for match in _PROSE_PATH.finditer(path.read_text("utf-8")):
            named = match.group(1)
            if any(character in named for character in _GLOB_CHARACTERS):
                continue
            if not names_something(named, here, files):
                out.append(f"{here}: names -> {named}")
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
        f"\n{len(problems)} reference(s) do not resolve. The ones under packages/*/src ship "
        "inside the wheel, so a reader follows them out of a released package.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
