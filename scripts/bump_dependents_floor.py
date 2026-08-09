"""Raise the maf-sandbox floor in the packages that depend on it, after a release.

`release-please.yml` runs this once `maf-sandbox` has published a version. It moves a
dependent's floor to that version **only when the dependent has already adopted it** — its
ceiling admits the version and its floor is a minor behind — because that is the one case the
post-release step exists for: the ceiling was widened in an earlier pull request to build
against the new version, and the floor could not point at it until it was on PyPI. A patch,
or a version a dependent's ceiling still excludes, moves nothing: pinning a newer floor than a
consumer needs would over-constrain them for no reason.

    python scripts/bump_dependents_floor.py <released-version>

Exits non-zero if a package that depends on maf-sandbox carries a constraint this cannot read
— editing by pattern silently no-ops when the string drifts, and a release step that quietly
does nothing while looking healthy is worse than one that fails. Prints the files it changed;
the workflow opens a pull request when there are any.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_CONSTRAINT = re.compile(r"maf-sandbox>=(\d+(?:\.\d+)*),<(\d+(?:\.\d+)*)")
# The distribution name at the head of a dependency string, before any version operator.
_DIST_NAME = re.compile(r"[A-Za-z0-9._-]+")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _admits(version: tuple[int, ...], ceiling: tuple[int, ...]) -> bool:
    """Whether ``version`` is below the ``<ceiling`` bound, comparing at equal width."""
    width = max(len(version), len(ceiling))
    return version + (0,) * (width - len(version)) < ceiling + (0,) * (
        width - len(ceiling)
    )


def parse_constraint(constraint: str) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    """The ``(floor, ceiling)`` of a ``maf-sandbox>=X,<Y`` constraint, or ``None`` if not that."""
    match = _CONSTRAINT.search(constraint)
    if match is None:
        return None
    return _version(match.group(1)), _version(match.group(2))


def bump_floor(text: str, released: tuple[int, ...]) -> tuple[str, bool]:
    """Rewrite a pyproject's maf-sandbox floor to ``released`` when the dependent has adopted it.

    Adopted means the ceiling admits the release and the floor is an older ``(major, minor)`` —
    the state a ceiling-widening pull request leaves behind while the version it named is being
    published. Anything else is returned unchanged.
    """
    parsed = parse_constraint(text)
    if parsed is None:
        return text, False
    floor, ceiling = parsed
    if not _admits(released, ceiling) or released[:2] <= floor[:2]:
        return text, False
    ceiling_text = _CONSTRAINT.search(text).group(2)  # type: ignore[union-attr]
    released_text = ".".join(str(part) for part in released)
    new_text = _CONSTRAINT.sub(
        f"maf-sandbox>={released_text},<{ceiling_text}", text, count=1
    )
    return new_text, new_text != text


def _base_dependency(dependencies: list[str]) -> str | None:
    """The dependency on the base ``maf-sandbox`` distribution exactly, or ``None``.

    Read from the parsed dependency string, not the file text, so it does not matter whether the
    pyproject quotes with ``"`` or ``'`` — and the exact name match keeps a dependency on the
    sibling ``maf-sandbox-acas`` from being taken for one on the base package.
    """
    for dep in dependencies:
        name = _DIST_NAME.match(dep.strip())
        if name is not None and name.group(0) == "maf-sandbox":
            return dep
    return None


def run(released_text: str, repo_root: Path) -> list[Path]:
    """Bump every adopting dependent under ``repo_root``; return the files changed. May exit."""
    released = _version(released_text)
    changed: list[Path] = []
    for path in sorted(repo_root.glob("packages/*/pyproject.toml")):
        text = path.read_text("utf-8")
        project = tomllib.loads(text).get("project", {})
        if project.get("name") == "maf-sandbox":
            continue
        base = _base_dependency(project.get("dependencies", []))
        if base is None:
            continue
        if parse_constraint(base) is None:
            raise SystemExit(
                f"{path}: depends on maf-sandbox but not as 'maf-sandbox>=X,<Y'; this script "
                "cannot bump it, and failing beats silently skipping a release-time step."
            )
        new_text, did = bump_floor(text, released)
        if did:
            path.write_text(new_text, "utf-8")
            changed.append(path)
    return changed


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <released-version>", file=sys.stderr)
        return 2
    changed = run(argv[1], Path(__file__).resolve().parent.parent)
    if changed:
        print("bumped the maf-sandbox floor in:")
        for path in changed:
            print(f"  {path.parent.name}")
    else:
        print("no dependent has adopted this version; nothing to bump")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
