"""One reader for the PEP 723 block every sample carries.

`uv run agent.py` resolves that block, so several checks have to read it. This is the reader
they share.

`tests/test_sample_metadata.py` deliberately keeps its own. That module is the block's
specification test — it asserts the block parses at all — and a specification test sharing a
parser with the thing it validates cannot tell a wrong parser from a wrong block.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = _ROOT / "samples"

#: PEP 723, as the spec writes it: a `script` block of `#`-prefixed lines.
BLOCK = re.compile(r"(?m)^# /// script\s*$\s(?P<body>(?:^#(?:| .*)$\s)+)^# ///\s*$")

#: The distribution a requirement names, before any specifier or extra.
_DISTRIBUTION = re.compile(r"[A-Za-z0-9._-]+")


def sample_directories() -> list[Path]:
    """Every sample directory, in the order their numbers give."""
    return sorted(path for path in SAMPLES.glob("[0-9][0-9]_*") if path.is_dir())


def block_body(agent: Path) -> str | None:
    """The block's TOML text with its comment prefix removed, or None if there is no block.

    None rather than a raise: one caller refuses, another skips, and the message each wants
    names its own subject.
    """
    match = BLOCK.search(agent.read_text(encoding="utf-8"))
    if not match:
        return None
    return "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("body").splitlines(keepends=True)
    )


def declared(agent: Path) -> dict | None:
    """The block as the TOML the spec says it is, or None if there is no block."""
    body = block_body(agent)
    return None if body is None else tomllib.loads(body)


def distribution(requirement: str) -> str | None:
    """The distribution ``requirement`` names, or None if it names none."""
    match = _DISTRIBUTION.match(requirement.strip())
    return match.group(0) if match else None
