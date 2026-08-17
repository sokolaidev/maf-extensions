"""Reading a comment beside a version constraint, for the two guards that have to.

Not a test module. `tests/` is otherwise standalone files, and this one exists because the
alternative was measured and failed: `test_release_config.py` and `test_sample_metadata.py`
each grew their own copy of "is there a release named in the prose next to this constraint",
and within a day the copies disagreed — one missed a trailing comment and a two-component
version, the other flagged the `1.0.0` that both are supposed to permit (#385). Two copies with
the same cases pinned is the cheaper coupling right up until one of them is edited.

The rule both guards enforce: `scripts/set_dependents_range.py` rewrites a constraint and never
the sentence beside it, so a comment naming a release is stale one release later and nothing
notices. The constraint is the source of truth; the comment says what the bound is *for*.
"""

from __future__ import annotations

import re

#: A pre-1.0 release of this project, named in prose.
#:
#: The lookbehind is the whole subtlety. `1.0.0` is deliberately legal — it is the stability
#: boundary every package's dependency comment refers to, not a pointer at a release — and
#: without `(?<![.\d])` the `0.0` inside it matches, so the guard fails on all five packages
#: for saying the one thing it means to allow. The three-component pattern this replaces was
#: safe against `1.0.0` only by accident, having no `0.x.y` substring to find, and paid for it
#: by missing `0.13` — which is how anyone writes a minor.
RELEASE_IN_PROSE = re.compile(r"(?<![.\d])0\.\d+(?:\.\d+)?")


def toml_comment(line: str) -> str:
    """Whatever a TOML line says after its `#`, leading or trailing, or `""`.

    Trailing counts. A guard reading only lines that *begin* a comment misses
    `"maf-sandbox-bicep",  # needs 0.14` — legal TOML, the most natural place to write the
    note, and on the very line the bump script rewrites.

    Quote-aware, because the `#` that opens a comment and the `#` inside a dependency string
    are the same character and only one of them is prose: without that, the fragment in
    `pkg @ https://host/w.whl#sha256=…` reads as a comment naming a version.
    """
    quoted = False
    for index, character in enumerate(line):
        if character == '"':
            quoted = not quoted
        elif character == "#" and not quoted:
            return line[index:]
    return ""


def release_named_in(line: str) -> str | None:
    """The release a TOML line's comment names, or ``None``. The two halves, applied."""
    found = RELEASE_IN_PROSE.search(toml_comment(line))
    return found.group(0) if found else None
