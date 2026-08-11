"""Widen the dependents' maf-sandbox ceiling to admit the minor after the one just released.

    python scripts/widen_dependents_ceiling.py <released-version>
    python scripts/widen_dependents_ceiling.py --print-target <released-version>

Releasing 0.7.0 sets every ceiling to `<0.9`, so the 0.8.0 after it is admitted before it
exists. `--print-target` prints that bound and changes nothing, so a caller naming it in a
branch or a title reads it from here rather than deriving it a second time. RELEASING.md has
the release order this belongs to.

Only ever widens, so re-running on a patch changes nothing. Exits non-zero if a dependent
carries a constraint it cannot read: a release step that quietly no-ops while looking healthy
is worse than one that stops.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

_CONSTRAINT = re.compile(r"maf-sandbox>=(\d+(?:\.\d+)*),<(\d+(?:\.\d+)*)")
#: The distribution name at the head of a dependency string, before any version operator.
_DIST_NAME = re.compile(r"[A-Za-z0-9._-]+")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def target_ceiling(released: tuple[int, ...]) -> tuple[int, ...]:
    """The bound that admits the next minor after ``released``.

    Two minors up, not one: a ceiling of ``<0.8`` excludes 0.8.0 itself, and admitting the
    next release is the whole point.
    """
    major, minor = (tuple(released) + (0, 0))[:2]
    return (major, minor + 2)


def widen(text: str, released: tuple[int, ...]) -> tuple[str, bool]:
    """Rewrite a pyproject's maf-sandbox ceiling when it is narrower than the target."""
    match = _CONSTRAINT.search(text)
    if match is None:
        return text, False
    current = _version(match.group(2))
    target = target_ceiling(released)
    if current >= target:
        return text, False
    floor_text = match.group(1)
    target_text = ".".join(str(part) for part in target)
    new_text = _CONSTRAINT.sub(
        f"maf-sandbox>={floor_text},<{target_text}", text, count=1
    )
    return new_text, new_text != text


def _base_dependency(dependencies: list[str]) -> str | None:
    """The dependency on the base ``maf-sandbox`` distribution exactly, or ``None``."""
    for dep in dependencies:
        name = _DIST_NAME.match(dep.strip())
        if name is not None and name.group(0) == "maf-sandbox":
            return dep
    return None


def run(released_text: str, repo_root: Path) -> list[Path]:
    """Widen every dependent under ``repo_root``; return the files changed. May exit."""
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
        if _CONSTRAINT.search(base) is None:
            raise SystemExit(
                f"{path}: depends on maf-sandbox but not as 'maf-sandbox>=X,<Y'; this script "
                "cannot widen it, and failing beats silently skipping a release-time step."
            )
        new_text, did = widen(text, released)
        if did:
            path.write_text(new_text, "utf-8")
            changed.append(path)
    return changed


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--print-target":
        print(".".join(str(part) for part in target_ceiling(_version(argv[2]))))
        return 0
    if len(argv) != 2:
        print(f"usage: {argv[0]} [--print-target] <released-version>", file=sys.stderr)
        return 2
    changed = run(argv[1], Path(__file__).resolve().parent.parent)
    if changed:
        target = ".".join(str(part) for part in target_ceiling(_version(argv[1])))
        print(f"widened the maf-sandbox ceiling to <{target} in:")
        for path in changed:
            print(f"  {path.parent.name}")
    else:
        print("every dependent already admits the next minor; nothing to widen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
