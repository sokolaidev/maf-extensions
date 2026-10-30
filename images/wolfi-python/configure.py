"""Apply the fixed CPython source release to a pinned Wolfi packaging recipe."""

import sys
from pathlib import Path


def configure(root: Path) -> None:
    """Keep Wolfi's ABI patches and tests while replacing its maintenance snapshot."""
    recipe = root / "python-3.13.yaml"
    source = recipe.read_text(encoding="utf-8")
    for before, after in (
        ('version: "3.13.17_git20261023"', 'version: "3.13.18"'),
        ("epoch: 1 # run upstream regrtest subset via shared pipeline", "epoch: 0"),
    ):
        if source.count(before) != 1:
            raise ValueError(f"Unexpected Wolfi recipe: {before}")
        source = source.replace(before, after)
    start = source.index("  - uses: git-checkout\n")
    end = source.index("  - name: Support OpenSSL 4 ABI\n", start)
    source = (
        source[:start]
        + """  - uses: git-checkout
    with:
      repository: https://github.com/python/cpython
      tag: v3.13.18
      expected-commit: 0d4bd512fc429e3d4eacdd113c975e161f21644d

"""
        + source[end:]
    )
    recipe.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    configure(Path(sys.argv[1]))
