"""Wait until *this* runner's PyPI edge serves the version the release just published.

Usage:
    python scripts/await_live_version.py maf-sandbox-docker 0.7.2

`wait-for-propagation` in publish-packages.yml confirms the same thing, and the gap is whose
edge it confirmed it on: PyPI's Simple index is an eventually-consistent CDN, so a version
visible to that runner is not a version visible to the one about to resolve. Seven live jobs
of one release started in the same second and split four to three on which version they got
(#595, the residual #563 named).

Read deliberately **without** cache-busting. The stale answer is a warm edge copy with an
unexpired TTL, so waiting it out is the whole job; a cache-busted read would go to the origin
and prove nothing about what `uv` is handed a moment later.

Runs before the sample rather than retrying after it: a retry would have to run the sample
again to be honest about what it tested, and that is a live model, a live container and a
billable sandbox. This costs seconds and fails before any of it is spent.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

#: Long enough for an edge TTL to lapse, short enough to fail inside the job's own timeout.
DEADLINE_SECONDS = 600
INTERVAL_SECONDS = 10
_SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"


def serves(payload: dict, package: str, version: str) -> str | None:
    """Why this index document does not yet offer ``version``, or None when it does.

    A wheel, not the sdist beside it: a resolver takes the wheel, and an sdist that landed
    first would only make this wait for something nothing here needs.
    """
    if version not in payload.get("versions", []):
        return "version not listed"
    prefix = f"{package.replace('-', '_')}-{version}-"
    names = [entry.get("filename", "") for entry in payload.get("files", [])]
    if not any(name.startswith(prefix) and name.endswith(".whl") for name in names):
        return "listed, but no wheel for it yet"
    return None


def read_index(package: str) -> dict:
    """The Simple index document for ``package``, as a resolver on this runner would get it."""
    request = urllib.request.Request(
        f"https://pypi.org/simple/{package}/", headers={"Accept": _SIMPLE_JSON}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def await_version(
    package: str,
    version: str,
    *,
    fetch=read_index,
    now=time.monotonic,
    sleep=time.sleep,
) -> int:
    """Poll until the edge offers ``version``; 0 when it does, 1 when the deadline passes."""
    deadline = now() + DEADLINE_SECONDS
    while True:
        try:
            reason = serves(fetch(package), package, version)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = f"index unreachable: {error}"
        except json.JSONDecodeError as error:
            # A 200 carrying half a document is an edge mid-write, which is the condition being
            # waited out rather than one to end on.
            reason = f"index returned unparseable JSON: {error}"
        if reason is None:
            print(f"{package} {version} is served here; running the sample")
            return 0
        if now() >= deadline:
            print(
                f"::error::this runner's PyPI edge still does not serve {package} {version} "
                f"after {DEADLINE_SECONDS}s ({reason}); the sample would resolve an older "
                "release and measure the wrong thing",
                file=sys.stderr,
            )
            return 1
        print(f"not here yet: {reason}; retrying in {INTERVAL_SECONDS}s")
        sleep(INTERVAL_SECONDS)


def main(argv: list[str]) -> int:
    """Wait for the runner's own edge to offer the published version before a sample resolves."""
    if len(argv) != 3:
        print(f"usage: {argv[0]} <package> <version>", file=sys.stderr)
        return 2
    return await_version(argv[1], argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
