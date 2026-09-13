"""The `uv run` arguments that decide whether a sample runs against the index or this checkout.

    python scripts/sample_source_args.py <sample-dir> [--source published|branch]

Prints nothing under `published`, which is the default and what a release verification wants:
`uv run --no-project` resolves the sample's PEP 723 block against PyPI, so what the job proves
is that the published wheels still work together — the claim `samples/README.md` says a sample
exists to make.

Under `branch` it prints one `--with <path>` per package of this repository the block names, so
uv builds those from the checkout and resolves everything else from the index exactly as before.
That is the pre-merge question instead: does *this* code still run the sample, before any of it
is published. `--with` is the only lever uv offers here — measured: `UV_OVERRIDE` is read by
`uv pip` and `uv sync` but not by `uv run` over a PEP 723 script, and neither `--with` nor
`--with-requirements` has an environment variable.

The packages are read out of the block rather than listed here. A sample that gains a dependency
gets it injected without this script being touched, and a sample cannot be handed a package it
never named — which is the difference between running this sample against the branch and running
it against a set nobody assembled.

A caller cannot tell the two modes apart from the installed version: a package's in-tree version
is the one it last released, because release-please bumps it only in a Release PR. What does
tell them apart is `direct_url.json` in the installed distribution, which only a path install
writes. So a job in `branch` mode must not assert the published version it resolved.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import sample_blocks

PUBLISHED = "published"
BRANCH = "branch"

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGES = _ROOT / "packages"


def repository_packages() -> frozenset[str]:
    """Every distribution this repository builds, by directory name."""
    return frozenset(
        path.name for path in _PACKAGES.glob("*") if (path / "pyproject.toml").is_file()
    )


def declared_packages(agent: Path) -> list[str]:
    """The repository's own distributions the sample's block names, in the block's order."""
    block = sample_blocks.declared(agent)
    if block is None:
        return []
    ours = repository_packages()
    named = (sample_blocks.distribution(entry) for entry in block.get("dependencies", []))
    return [name for name in named if name in ours]


def arguments(agent: Path, source: str) -> list[str]:
    """The `uv run` arguments for ``source``; empty for anything but ``branch``."""
    if source != BRANCH:
        return []
    return [word for name in declared_packages(agent) for word in ("--with", f"./packages/{name}")]


def main(argv: list[str]) -> int:
    """CLI entry: print the `uv run` arguments that run this sample against ``--source``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path)
    parser.add_argument("--source", choices=(PUBLISHED, BRANCH), default=PUBLISHED)
    parsed = parser.parse_args(argv[1:])

    agent = parsed.sample / "agent.py"
    if not agent.is_file():
        print(f"no sample at {agent.as_posix()}", file=sys.stderr)
        return 1
    # A branch run that injects nothing would be a published run wearing its name, and the job
    # would report the wrong thing about what it tested. Refuse rather than print an empty line.
    words = arguments(agent, parsed.source)
    if parsed.source == BRANCH and not words:
        print(
            f"{agent.as_posix()} names none of this repository's packages, so there is "
            "nothing to run from the branch",
            file=sys.stderr,
        )
        return 1
    print(" ".join(words))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
