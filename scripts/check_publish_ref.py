"""Require PyPI provenance to name the exact package release tag."""

from __future__ import annotations

import argparse


def main(argv: list[str] | None = None) -> int:
    """Refuse a production upload from any ref other than its release tag."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("pypi", "testpypi"))
    parser.add_argument("package")
    parser.add_argument("version")
    parser.add_argument("ref")
    args = parser.parse_args(argv)
    if args.target == "testpypi":
        return 0
    expected = f"refs/tags/{args.package}-v{args.version}"
    if args.ref != expected:
        print(
            f"::error::PyPI upload requires {expected}; workflow ref is {args.ref!r}. "
            "Re-run the failed release-tag workflow after its gate clears, or dispatch "
            "at that tag. Branch dispatches must target testpypi."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
