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
from urllib.parse import parse_qs, unquote, urlsplit

#: Markdown inline links and images, capturing the target. Reference-style definitions
#: (`[label]: target`) are matched separately below; both spellings resolve the same way.
#:
#: A destination wrapped in angle brackets runs to the `>`, so `[x](<a file.md>)` is one target
#: and not the two words it looks like; the bare spelling stops at the first space, which is
#: what makes the brackets necessary. Both alternatives capture, so a match yields one group
#: filled and one empty.
_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(?:<([^>\n]*)>|([^)\s]+))")
_REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(?:<([^>\n]*)>|(\S+))", re.MULTILINE)

#: A path into this repository, written in prose. Anchored on a top-level directory and
#: required to end in an extension, because those two together are what make it a *path* rather
#: than a phrase — `packages/maf-sandbox` is a directory a sentence may name loosely, and
#: `docs/sandbox/architecture.md` is a thing a reader is being told to open.
#:
#: The extension is unbounded in length and must start with a letter: `.bicep`, `.yaml` and
#: `.toml` name files, and `maf-sandbox/0.19.0` names a version.
#:
#: The lookbehind stops a match starting midway through a longer path, so `vendor/docs/a.md`
#: is one reference and not also `docs/a.md`. It is not what keeps URLs out — a query string
#: puts a repo-shaped path after `=`, which no lookbehind on the preceding character can tell
#: from a bare mention — so `without_urls` removes those spans before this runs at all.
_PROSE_PATH = re.compile(
    r"(?<![\w./-])((?:docs|packages|samples|scripts|tests)/[\w./-]+\.[A-Za-z][A-Za-z0-9]*)"
    r"(?![\w/])"
)

#: An absolute URL, whose path names somebody else's tree.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s)>\]]+")

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

#: A setext heading: text underlined by `=` or `-`. GitHub renders it as a heading and gives it
#: the slug the ATX spelling of the same text would get, so a link to one is as ordinary as a
#: link to the other and refusing it reports a working link.
_SETEXT = re.compile(r"^ {0,3}(?P<text>\S[^\n]*?)[ \t]*\n {0,3}(?:=+|-+)[ \t]*$", re.MULTILINE)

#: An explicit anchor a document plants for itself, which no heading slug would produce.
_HTML_ANCHOR = re.compile(r"""<a\s[^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

#: An HTML comment, which GitHub renders as nothing whatsoever.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: A run of backticks, opening or closing an inline code span.
_TICKS = re.compile(r"`+")

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
    found = _INLINE_LINK.findall(text) + _REFERENCE_LINK.findall(text)
    return [angled or bare for angled, bare in found]


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
    """``target`` as a path: no fragment, no query, and percent-decoded.

    A link destination is a URL even when it is relative, so `docs/a%20file.md` names the
    tracked `docs/a file.md`. Comparing the encoded spelling against the tracked tree finds
    nothing and reports a working link as a missing file.
    """
    return unquote(target.split("#", 1)[0].split("?", 1)[0])


def _blank(kept: list[str], start: int, end: int) -> None:
    """Overwrite ``kept[start:end]`` with spaces, leaving line breaks where they were.

    Preserving the breaks is what keeps every later pattern line-anchored: a span blanked into
    one long run of spaces would join the line after it to the line before.
    """
    for index in range(start, end):
        if kept[index] != "\n":
            kept[index] = " "


def without_code_spans(text: str) -> str:
    """``text`` with every inline code span blanked, its length and line breaks preserved.

    A code span holds characters rather than markup, so `` `[x](missing.md)` `` is a document
    showing its reader what a link looks like — the inline half of the rule fenced blocks
    already get. A backtick run with no matching closer is literal text and is left alone,
    which is what stops one stray tick blanking the rest of a page.
    """
    kept = list(text)
    position = 0
    while (opener := _TICKS.search(text, position)) is not None:
        closer, search = None, opener.end()
        while (candidate := _TICKS.search(text, search)) is not None:
            if candidate.group(0) == opener.group(0):
                closer = candidate
                break
            search = candidate.end()
        if closer is None:
            position = opener.end()
            continue
        _blank(kept, opener.start(), closer.end())
        position = closer.end()
    return "".join(kept)


def without_html_comments(text: str) -> str:
    """``text`` with every HTML comment blanked, its line breaks preserved.

    GitHub renders a comment as nothing, so an anchor inside one plants no target and a link
    to it is dead however complete the markup looks.
    """

    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _HTML_COMMENT.sub(blank, text)


def without_urls(text: str) -> str:
    """``text`` with every absolute URL blanked, its length and line breaks preserved.

    A URL's path belongs to another tree, and its query can put a repo-shaped path anywhere —
    `?file=docs/gone.md` leaves `docs/gone.md` sitting after an `=`, which reads exactly like a
    bare mention. Removing the span is the only way to tell the two apart.
    """
    kept = list(text)
    for match in _URL.finditer(text):
        _blank(kept, match.start(), match.end())
    return "".join(kept)


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
        # CommonMark: a closer is the same character as the opener, at least as long, and
        # carries no info string. A line failing any of the three is content — ```python
        # inside an open block is a line of a sample, not the end of one — and reading it as
        # the closer hands the rest of the block to the heading and link readers as markdown.
        if found and run[0] == opener[0] and len(run) >= len(opener):
            if not line[found.end() :].strip():
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


def headings(prose: str) -> list[str]:
    """Every heading's text in ``prose``, in document order, in both spellings GitHub renders.

    Order is what the numbering below depends on, so the two patterns are merged by position
    rather than concatenated — a setext heading between two ATX ones is the second of three.
    """
    found = [(match.start(), match.group(2)) for match in _HEADING.finditer(prose)]
    found += [(match.start(), match.group("text")) for match in _SETEXT.finditer(prose)]
    return [text for _position, text in sorted(found, key=lambda pair: pair[0])]


def anchors(text: str) -> set[str]:
    """Every fragment ``text`` can be linked to: its heading slugs and its explicit anchors.

    Repeated headings are numbered the way GitHub numbers them — the second ``## Notes`` is
    ``#notes-1`` — because a document with two sections of the same name is exactly where a
    reader needs the link to be right.

    Headings are read from the prose as written, because a code span inside one contributes its
    text to the slug. Explicit anchors are read from prose with comments and code spans removed,
    because an `<a id>` in either of those plants nothing a link can reach.
    """
    prose = without_fenced_blocks(text)
    found: set[str] = set()
    for heading in headings(prose):
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
    found.update(_HTML_ANCHOR.findall(without_code_spans(without_html_comments(prose))))
    return found


def _repo_relative(destination: Path, repo_root: Path) -> str | None:
    """``destination`` as a repo-relative POSIX path, or None when it leaves the tree."""
    try:
        return destination.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None


def _fragment(target: str) -> str:
    """The part after ``#``, percent-decoded: a place inside a document rather than a document.

    A fragment is URL-encoded on the wire, so `#caf%C3%A9` is what a browser sends for the
    heading GitHub slugged as `café`. Matching the encoded spelling against the slug fails and
    reports a link that works.
    """
    return unquote(target.split("#", 1)[1]) if "#" in target else ""


def asks_for_the_plain_view(target: str) -> bool:
    """Whether ``target``'s query asks GitHub to serve the file as source rather than markdown.

    Only `plain=1` does. It matters because that view is a line-numbered source listing and
    carries the `#L42` anchors the rendered page has not, so it is the one case where a line
    reference on a markdown file is live.
    """
    query = urlsplit(target.split("#", 1)[0]).query
    return "1" in parse_qs(query).get("plain", [])


def broken_links(repo_root: Path) -> list[str]:
    """One line per markdown link whose file, or whose heading, is not in the tracked tree."""
    files, directories = tracked_tree(repo_root)
    out: list[str] = []
    for path in tracked(repo_root, "*.md"):
        # A link is only read from the parts of the page that render as markdown. A fenced
        # block and a code span both hold text *about* a link rather than a link, and gating on
        # either reds a document for showing its reader what one looks like.
        text = without_code_spans(without_fenced_blocks(path.read_text("utf-8")))
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
            # fragment is dead. `?plain=1` is the exception and the only one: it asks for the
            # source listing, which is line-numbered. Any other query still renders markdown.
            if _LINE_REFERENCE.match(fragment) and asks_for_the_plain_view(target):
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
        for match in _PROSE_PATH.finditer(without_urls(path.read_text("utf-8"))):
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
