"""Configuration scaffolding for this sample. Not part of the library, and not an example.

Every sample carries a byte-identical copy of this file, and `tests/test_sample_scaffold.py`
holds them that way. Copying is deliberate: a sample has to run from a directory the reader
downloaded, against wheels from PyPI, with nothing installed from this repository — so a
shared module would have to be published, and publishing it would make it API. `sys.path[0]`
is the script's directory, which is what lets `from _scaffold import ...` work at all.

`require_env_vars` is here rather than in `maf-sandbox` for a reason worth stating, since the
line count argues the other way: it prints to stderr and returns a sentinel for `main` to exit
on. That is a script's job. A library helper that did it would be reached for by an
application, where printing to stderr and exiting is exactly wrong.
"""

from __future__ import annotations

import os
import sys


def require_env_vars(names: tuple[str, ...]) -> dict[str, str] | None:
    """Read `names` from the environment, or report every one that is missing.

    Worth doing before anything else, and worth failing on.  A kind's tool factory returns an
    empty list when the router has no backend, so a half-configured run does not crash — it
    quietly produces an agent with no tools, which answers the question from the model alone.
    That is the T0 behaviour these samples exist to contrast with, and it is indistinguishable
    from success unless someone says so out loud.
    """
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print("Not configured. These environment variables are unset:", file=sys.stderr)
        for name in missing:
            print(f"  {name}", file=sys.stderr)
        print("\nSee this directory's README.md.", file=sys.stderr)
        return None
    return {name: os.environ[name] for name in names}
