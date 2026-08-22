"""Say whether a change touches anything the code checks need to run for.

    git diff --name-only <base> HEAD | python scripts/changed_paths.py

Prints one ``key=value`` line for ``$GITHUB_OUTPUT``. A pull request that edits only
documentation does not need the offline suite, both pyright passes, the wheel builds or the
Docker integration job — but it does need the checks to *report*, because both job names are
required status checks and a context that never reports blocks the merge for ever (#560).
So the jobs still run and skip their expensive steps; this is what they skip on.

**Documentation, here, means a file no wheel carries.** ``packages/**`` is deliberately code
whatever its extension: `packages/maf-sandbox/README.md` is packaged and rendered on PyPI, so a
change to it is a change to a published artefact and gets the full suite. Everything under
``docs/``, and every ``.md`` outside ``packages/``, is documentation.

Silence is read as code. An empty diff, a path this cannot classify, a failure to work out the
base — each runs everything, because the cost of running the suite unnecessarily is three
minutes and the cost of skipping it wrongly is a defect on `main`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable


def is_documentation(path: str) -> bool:
    """Whether ``path`` is a file that no published wheel carries.

    The ``packages/`` exclusion is the whole of the subtlety. A README under a package is
    documentation by every ordinary reading and is *shipped* by this one, which is the reading
    that decides whether a wheel build has to run.
    """
    normalised = path.strip().replace("\\", "/")
    if not normalised:
        return False
    if normalised.startswith("packages/"):
        return False
    return normalised.startswith("docs/") or normalised.endswith(".md")


def runs_code_checks(paths: Iterable[str]) -> bool:
    """Whether anything in ``paths`` needs the suite, the type passes and the wheel builds.

    True when *any* path is not documentation, and true for an empty change — a diff that came
    back empty is more likely a base this could not resolve than a pull request that changed
    nothing.
    """
    listed = [path for path in (line.strip() for line in paths) if path]
    if not listed:
        return True
    return any(not is_documentation(path) for path in listed)


def main(argv: list[str]) -> int:
    """CLI entry: read changed paths on stdin, print ``code=true`` or ``code=false``."""
    if len(argv) != 1:
        print(f"usage: git diff --name-only <base> HEAD | {argv[0]}", file=sys.stderr)
        return 2
    print(f"code={'true' if runs_code_checks(sys.stdin) else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
