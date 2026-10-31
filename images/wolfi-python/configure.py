"""Apply the fixed CPython maintenance snapshot to a pinned Wolfi packaging recipe."""

import sys
from pathlib import Path


def configure(root: Path) -> None:
    """Keep Wolfi's ABI patches and tests while updating its maintenance snapshot."""
    recipe = root / "python-3.13.yaml"
    source = recipe.read_text(encoding="utf-8")
    for before, after in (
        ('version: "3.13.17_git20261023"', 'version: "3.13.18_git20261030"'),
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
      branch: 3.13
      expected-commit: 42155a0feaca9ba6991942c323cbe9fb7278be3c

  - name: Drop the branch development marker from the version string
    runs: |
      sed -i 's/3[.]13[.]18+/3.13.18/' Include/patchlevel.h
      grep -q '^#define PY_VERSION[[:space:]]*"3[.]13[.]18"' Include/patchlevel.h

"""
        + source[end:]
    )
    # Consumers need the normal interpreter, without a file-capability override.
    start = source.index('  - name: "${{package.name}}-privileged-netbindservice"\n')
    end = source.index('  - name: "${{package.name}}-tk"\n', start)
    source = source[:start] + source[end:]
    recipe.write_text(source, encoding="utf-8")


if __name__ == "__main__":
    configure(Path(sys.argv[1]))
