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
from typing import NamedTuple
from urllib.parse import parse_qs, unquote, urlsplit

#: Whitespace that may sit between a link's `(` and its destination.
_SPACE = " \t\n"

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

#: A backslash escape: markdown syntax, and the punctuation after it is the literal character.
_BACKSLASH_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")

#: A URI in prose, whose payload names somebody else's tree. Three shapes: any scheme with
#: `//`, the protocol-relative form, which has no scheme and is no less external for it, and
#: the schemes below, which carry a payload without one.
#:
#: That last group is a list rather than `[A-Za-z][A-Za-z0-9+.-]*:` on purpose. `is_local` can
#: use the general form because it judges a whole destination, where everything before the
#: colon is the scheme by construction. Prose has no such boundary: `:class:`SandboxSpec`` and
#: `:func:`run`` are the same shape, and this repository's shipped docstrings hold 461 of them.
#: Blanking on the general rule would swallow whatever followed the colon in every one.
_URI_SCHEMES = "mailto|data|file|ftp|ftps|tel|sms|news|nntp|urn|git|ssh|irc|ircs|magnet|about"
_URL = re.compile(rf"(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^\s)>\]]+|\b(?:{_URI_SCHEMES}):[^\s)>\]]+")

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

#: A setext heading: a paragraph underlined by `=` or `-`. GitHub renders it as a heading with
#: the slug the ATX spelling of that text would get, so a link to one is as ordinary as a link
#: to the other and refusing it reports a working link.
_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_SETEXT_TEXT = re.compile(r"^ {0,3}(\S.*?)[ \t]*$")

#: The `>` markers opening a blockquote, however deeply nested. A heading inside one is still a
#: heading — GitHub renders `> ### Title` with the anchor `#title` — and the markers are not
#: indentation, so they are removed rather than counted against the three spaces allowed above.
_BLOCKQUOTE = re.compile(r"^ {0,3}(?:> ?)+")

#: An explicit anchor a document plants for itself, which no heading slug would produce.
_HTML_ANCHOR = re.compile(r"""<a\s[^>]*\b(?:id|name)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

#: An HTML comment, which GitHub renders as nothing whatsoever.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

#: A run of backticks, opening or closing an inline code span.
_TICKS = re.compile(r"`+")

#: Inline markup that is *not* part of a heading's text: code ticks and emphasis runs.
#: Underscore is deliberately absent, though it is an emphasis marker too: it is also a word
#: character GitHub keeps, and the headings here are full of identifiers.
_MARKUP = re.compile(r"[`*~]")

#: Underscore emphasis, which the run above cannot strip blindly. An underscore is a word
#: character GitHub keeps, so `## host_tool_calls_over_exec` slugs with it — but `## _Important_` is
#: emphasis and slugs to `important`. Only a *paired* run at a word boundary is markup: an
#: unmatched `_private` emphasises nothing and stays, and an intraword `_` is never a
#: delimiter, which is what keeps the identifiers in these headings intact.
_UNDERSCORE_EMPHASIS = re.compile(r"(?<![0-9A-Za-z_])(_{1,3})(?=\S)(.+?)(?<=\S)\1(?![0-9A-Za-z_])")

#: `#L42` and `#L42-L60` on a source file are GitHub line references, not headings.
_LINE_REFERENCE = re.compile(r"^L\d+(?:-L\d+)?$")

#: A URI scheme opening a destination, which puts it off this tree.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


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


def _closing_bracket(text: str, opener: int) -> int | None:
    """Index of the ``]`` closing the ``[`` at ``opener``, or None when nothing closes it.

    Markdown lets a link label hold balanced brackets, so `[see [details]](x.md)` is one link
    whose label is `see [details]`. Stopping at the first `]` finds no `(` after it, reads the
    whole construct as not-a-link, and lets the target through unchecked — a *dead* target as
    readily as a live one, which is the failure this check exists to prevent.
    """
    depth = 0
    index = opener
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _escaped(text: str, index: int) -> bool:
    """Whether the character at ``index`` is escaped, by the parity of the backslashes before it.

    An odd run escapes; an even run is escaped backslashes and leaves the character active. So
    `\\[x](a.md)` is literal text, while `\\\\[x](a.md)` renders a backslash *and a live link* —
    testing only the character in front reads the second as the first and skips a real target.
    """
    run = 0
    while index - run - 1 >= 0 and text[index - run - 1] == "\\":
        run += 1
    return run % 2 == 1


def _after_title(text: str, index: int) -> int:
    """The index past any whitespace and optional title sitting between a destination and ``)``."""
    while index < len(text) and text[index] in _SPACE:
        index += 1
    closer = {'"': '"', "'": "'", "(": ")"}.get(text[index : index + 1])
    if closer is None:
        return index
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == closer:
            index += 1
            break
        index += 1
    while index < len(text) and text[index] in _SPACE:
        index += 1
    return index


def _unescaped(target: str) -> str:
    """``target`` with markdown's backslash escapes resolved to the characters they name.

    A backslash before ASCII punctuation is markdown syntax, not part of the path, so
    `docs/a_\\(draft\\).md` names the tracked `docs/a_(draft).md`. It is undone here rather
    than in `resolve`, because this is the markdown layer and percent-decoding is the URL one:
    a `%28` written after an escaped paren must survive this pass to be decoded by that one.
    """
    return _BACKSLASH_ESCAPE.sub(r"\1", target)


def _destination(text: str, paren: int) -> tuple[str, int, int, int] | None:
    """The ``(`` at ``paren`` read as a destination: its text, its bounds, and what follows.

    Two spellings. In angle brackets it runs to the ``>`` and may hold spaces. Bare, it runs to
    whitespace or to an *unbalanced* ``)`` — balanced pairs belong to the destination, so
    `docs/a_(draft).md` is one path and truncating it at the first ``)`` reports a file that is
    tracked as missing.

    The bounds cover the destination alone, never the label: `[docs/gone.md](docs/live.md)`
    renders that label as visible prose, and a pass that blanks the whole link stops the prose
    scan from ever reading it.

    None when nothing closes the ``(``. An unclosed one is not a link at all: GitHub renders
    `[x](missing.md` as those literal characters, so resolving the text after the paren reports
    a page for prose that points nowhere.
    """
    index = paren + 1
    while index < len(text) and text[index] in _SPACE:
        index += 1
    if index < len(text) and text[index] == "<":
        end = text.find(">", index + 1)
        if end == -1:
            return None
        begin, stop, target, index = index + 1, end, text[index + 1 : end], end + 1
    else:
        depth, begin = 0, index
        while index < len(text):
            character = text[index]
            if character == "\\":
                index += 2
                continue
            if character in _SPACE:
                break
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    break
                depth -= 1
            index += 1
        stop = min(index, len(text))
        target = text[begin:stop]
    after = _after_title(text, index)
    if text[after : after + 1] != ")":
        return None
    return _unescaped(target), begin, stop, after + 1


class Link(NamedTuple):
    """One markdown link: what it points at, what it shows, and two spans a later pass needs.

    ``start`` and ``end`` bound the whole link, which is what lets a heading be reduced to the
    words it renders. ``hidden`` bounds the part the prose scan must not read a second time —
    an inline link's destination, or a reference definition's whole line, because that line
    renders nothing at all and its label is not prose a reader ever sees.
    """

    target: str
    label: str
    start: int
    end: int
    hidden_start: int
    hidden_end: int


def inline_links(text: str, offset: int = 0) -> list[Link]:
    """Every inline link and image in ``text``.

    Scanned rather than matched, because both halves of `[label](destination)` nest and a flat
    pattern gets each one wrong in a different direction. ``offset`` carries the bounds out of
    a nested scan and back into the coordinates of the document.
    """
    found: list[Link] = []
    index = 0
    while (opener := text.find("[", index)) != -1:
        if _escaped(text, opener):
            index = opener + 1
            continue
        close = _closing_bracket(text, opener)
        if close is None:
            break
        parsed = _destination(text, close + 1) if text[close + 1 : close + 2] == "(" else None
        if parsed is None:
            # Not a link, but its label may hold one — `[see [x](a.md)]` is a bracketed
            # sentence around a real link. Stepping past the whole label would skip it.
            index = opener + 1
            continue
        target, begin, stop, index = parsed
        if target:
            found.append(
                Link(
                    target,
                    text[opener + 1 : close],
                    offset + opener,
                    offset + index,
                    offset + begin,
                    offset + stop,
                )
            )
        # The label is scanned too, because a link may wrap an image — `[![alt](i.png)](a.md)`
        # is the shape of every badge in this repository, and it names *two* files. Stepping
        # straight past the label checks the outer target and silently drops the inner one.
        found.extend(inline_links(text[opener + 1 : close], offset + opener + 1))
    return found


def inline_link_targets(text: str) -> list[str]:
    """Every inline link and image target in ``text``."""
    return [link.target for link in inline_links(text)]


def outermost(links: list[Link]) -> list[Link]:
    """``links`` with the ones nested inside another dropped, in document order.

    `inline_links` reports a badge's inner image as well as the link around it, so the spans
    overlap. Splicing text by overlapping spans duplicates or loses characters, and only the
    outer one is the unit a reader sees.
    """
    kept: list[Link] = []
    for link in sorted(links, key=lambda one: one.start):
        if not kept or link.start >= kept[-1].end:
            kept.append(link)
    return kept


def reference_links(text: str) -> list[Link]:
    """Every reference-style definition — `[label]: destination` — in ``text``.

    The label nests the same way an inline one does, so it is scanned the same way, and the
    destination carries the same backslash escapes.
    """
    found: list[Link] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        start = offset
        offset += len(line)
        line = line.rstrip("\r\n")
        indent = len(line) - len(line.lstrip(" "))
        if indent > 3 or line[indent : indent + 1] != "[":
            continue
        close = _closing_bracket(line, indent)
        if close is None or line[close + 1 : close + 2] != ":":
            continue
        label, rest = line[indent + 1 : close], line[close + 2 :].strip()
        target = ""
        if rest.startswith("<"):
            end = rest.find(">")
            if end != -1:
                target = rest[1:end]
        elif rest.split():
            target = rest.split(maxsplit=1)[0]
        if target:
            bounds = (start + indent, start + len(line))
            found.append(Link(_unescaped(target), label, *bounds, *bounds))
    return found


def reference_link_targets(text: str) -> list[str]:
    """Every reference-style definition target — `[label]: destination` — in ``text``."""
    return [link.target for link in reference_links(text)]


def without_link_destinations(text: str) -> str:
    """``text`` with every markdown link blanked, its length and line breaks preserved.

    A destination is `broken_links`' business, and it resolves one *relative to the document
    holding it*. The prose pass resolves against the repository root instead, so reading the
    same span twice reports one dangling reference as two — and from a nested document the
    second reading resolves a different file than the reader would reach.

    Only the destination is blanked from an inline link, never its label: `[docs/gone.md](x.md)`
    puts that path on the rendered page as text, and the prose pass is the only thing that
    reads it. A reference definition renders nothing at all, so the whole line goes.
    """
    kept = list(text)
    for link in inline_links(text) + reference_links(text):
        _blank(kept, link.hidden_start, link.hidden_end)
    return "".join(kept)


def link_targets(text: str) -> list[str]:
    """Every link target in ``text``, both inline and reference-style."""
    return inline_link_targets(text) + reference_link_targets(text)


def is_local(target: str) -> bool:
    """Whether ``target`` names something in this repository rather than elsewhere.

    A scheme — `https:`, `mailto:` — points off the tree entirely and is skipped rather than
    resolved. So is the protocol-relative form, `//example.invalid/docs`, which carries no
    scheme to match on and is no more ours for it.

    So is a *root-relative* destination. GitHub does not rewrite one to the repository root:
    `[x](/docs/foo)` on a rendered page goes to `https://github.com/docs/foo`, which is a page
    about somebody else's account. Resolving it here against the tracked tree reports a link
    that is wrong in a way this check has no standing to judge.
    """
    return bool(target) and not target.startswith("/") and not _SCHEME.match(target)


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


def rendered(text: str) -> str:
    """``text`` reduced to what GitHub renders as markdown: no fenced blocks, no comments.

    Every pass reads through this one, so the two rules hold everywhere rather than wherever
    they were remembered. A comment holds whatever its author stopped publishing, so a heading
    inside one plants no anchor and a link inside one asks nobody to follow it — read either as
    live and the check accepts a dead fragment in the first case and reports a working page in
    the second.

    Code spans are *not* removed here. They are markup to a link and text to a heading, so they
    are stripped by the passes that need it and never by this one.
    """
    return without_html_comments(without_fenced_blocks(text))


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

    Blockquote markers are stripped before the fence is matched, and the closer must come from
    the same quote depth as the opener. Every later pass reads past a `>`, so a quoted fence
    must be removed here or its contents reach them as markdown; and a closer in a different
    container is content, or a top-level block ends at the first quoted fence line inside it.
    """
    kept: list[str] = []
    opener: str | None = None
    opener_depth = 0
    for line in text.splitlines():
        bare = _BLOCKQUOTE.sub("", line)
        depth = line[: len(line) - len(bare)].count(">")
        found = _FENCE.match(bare)
        run = found.group(1) if found else ""
        if opener is None:
            if found:
                opener, opener_depth = run, depth
                kept.append("")
                continue
            kept.append(line)
            continue
        kept.append("")
        # CommonMark: a closer is the same character as the opener, at least as long, carries
        # no info string, and sits in the same container. A line failing any of the four is
        # content — an info-string line inside an open block is a line of a sample, not the end
        # of one — and reading it as the closer hands the rest of the block to every reader.
        if found and depth == opener_depth and run[0] == opener[0] and len(run) >= len(opener):
            if not bare[found.end() :].strip():
                opener = None
    return "\n".join(kept)


def visible_text(text: str) -> str:
    """``text`` with each link replaced by what it shows a reader, and each image by nothing.

    A heading is slugged from what renders, so `## [A link](docs/a.md)` is `#a-link`. Finding
    where the link ends is the whole difficulty, and it is the same difficulty the scanner
    already solves: a pattern that stops at the first `)` leaves `.md)` in the slug of
    `[A link](docs/a_(draft).md)`, and mangles a nested label worse than that.

    An image contributes no text at all — GitHub renders `<img>`, and its alt is an attribute
    rather than words on the page — so a link wrapping one slugs to nothing.
    """
    out: list[str] = []
    index = 0
    for link in outermost(inline_links(text)):
        image = link.start > 0 and text[link.start - 1] == "!"
        out.append(text[index : link.start - 1 if image else link.start])
        if not image:
            out.append(visible_text(link.label))
        index = link.end
    out.append(text[index:])
    return "".join(out)


def slugify(heading: str) -> str:
    """The fragment GitHub derives from a heading's text.

    Lowercase, strip everything that is not a word character, whitespace or a hyphen, then turn
    each remaining whitespace character into a hyphen — *each*, not each run, which is why
    ``## A — B`` becomes ``a--b`` and matching it any other way reports a working link.

    Backticks, asterisks and tildes go unconditionally, because the punctuation strip would
    remove them anyway. Underscores cannot: they survive that strip, so they are removed only
    where they are emphasis and kept everywhere else.
    """
    text = visible_text(heading)
    text = _UNDERSCORE_EMPHASIS.sub(r"\2", text)
    text = _MARKUP.sub("", text)
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    return re.sub(r"\s", "-", text)


def headings(prose: str) -> list[str]:
    """Every heading's text in ``prose``, in document order, in both spellings GitHub renders.

    Read line by line, with blockquote markers stripped first, because a heading inside a
    blockquote renders and carries an anchor like any other. Order is what the numbering above
    depends on: a setext heading between two ATX ones is the second of three.
    """
    lines = [_BLOCKQUOTE.sub("", line) for line in prose.splitlines()]
    found: list[str] = []
    for number, line in enumerate(lines):
        atx = _HEADING.match(line)
        if atx:
            found.append(atx.group(2))
            continue
        if not number or not _UNDERLINE.match(line):
            continue
        # An underline turns the paragraph above it into one heading — the *whole* paragraph,
        # not its last line, so `First line\nSecond line\n---` is `#first-line-second-line`.
        # Keeping only the final line rejects that anchor and accepts `#second-line`, which
        # GitHub never creates: wrong in both directions from the same mistake.
        paragraph: list[str] = []
        for previous in reversed(lines[:number]):
            # A blank line ends the paragraph. An ATX heading or another underline is not part
            # of one at all, so anything above it belongs to something else.
            if _HEADING.match(previous) or _UNDERLINE.match(previous):
                break
            text = _SETEXT_TEXT.match(previous)
            if text is None:
                break
            paragraph.append(text.group(1))
        if paragraph:
            found.append("\n".join(reversed(paragraph)))
    return found


def anchors(text: str) -> set[str]:
    """Every fragment ``text`` can be linked to: its heading slugs and its explicit anchors.

    Repeated headings are numbered the way GitHub numbers them — the second ``## Notes`` is
    ``#notes-1`` — because a document with two sections of the same name is exactly where a
    reader needs the link to be right.

    Headings are read from the prose as written, because a code span inside one contributes its
    text to the slug. Explicit anchors are read from prose with comments and code spans removed,
    because an `<a id>` in either of those plants nothing a link can reach.
    """
    prose = rendered(text)
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
    found.update(_HTML_ANCHOR.findall(without_code_spans(prose)))
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
        text = without_code_spans(rendered(path.read_text("utf-8")))
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
        text = path.read_text("utf-8")
        # Only markdown gets markdown preprocessing. An HTML comment renders as nothing, so a
        # path inside one asks no reader to open it; in a `.py` file `<!--` is just characters,
        # and treating it as a comment opener would blank shipped source to the next `-->`.
        if path.suffix.lower() == ".md":
            text = without_html_comments(text)
        prose = without_link_destinations(without_urls(text))
        for match in _PROSE_PATH.finditer(prose):
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
