"""Wait until `uv` on this runner can resolve the version the release just published.

Usage:
    python scripts/await_live_version.py maf-sandbox-docker 0.7.2

PyPI's Simple index is served through an eventually-consistent CDN, so a version visible to
one observer is not a version visible to the next. `wait-for-propagation` in
publish-packages.yml confirms the upload from its own runner, which is one layer of that; this
was added for the layer below it, where each sample job resolves on a runner of its own (#595).

**The probe has to be `uv`, and that is the whole lesson of this file.** Its first version
polled the index with `urllib` and a JSON `Accept`, passed, and the sample still resolved the
previous release *0.7 seconds later* on the same runner: a different client sends a different
`Accept`, a different `Accept` is a different CDN cache object, and the two go stale
independently. A pass by one HTTP client says nothing about another.

So the probe runs the same subcommand the sample does, which buys three things a hand-rolled
request cannot. It reads whatever representation `uv` reads. It downloads the wheel, so an
edge listing a version whose artifact it cannot yet serve does not pass. And it leaves `uv`'s
own cache holding that answer — measured: a `uv run` after this makes zero index requests,
against eleven without it — so the sample resolves from the bytes this checked rather than
from a second lookup that could land anywhere.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

#: Long enough for an edge to catch up, short enough to fail inside the job's own timeout.
DEADLINE_SECONDS = 600
INTERVAL_SECONDS = 10
#: One attempt: a resolve and a wheel fetch, a few seconds when the edge is current.
ATTEMPT_TIMEOUT_SECONDS = 180


def command(package: str, version: str) -> list[str]:
    """The probe: resolve and install exactly this version, refusing `uv`'s cached answer.

    `--refresh` so a cached miss cannot make the loop spin forever against its own memory; the
    attempt that succeeds refreshes too, which is what leaves the cache holding a fresh answer.
    """
    return [
        "uv",
        "run",
        "--no-project",
        "--refresh",
        "--with",
        f"{package}=={version}",
        "python",
        "-c",
        "pass",
    ]


#: uv colourises stderr even when it is a pipe, and wraps one sentence over several lines.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
#: The box-drawing it frames an error with, which carries nothing once the lines are joined.
_GLYPHS = str.maketrans("", "", "×╰─▶│")


def why_not(completed: subprocess.CompletedProcess) -> str:
    """`uv`'s own complaint, decolourised and folded onto one line.

    Taking the last line instead loses the sentence: uv wraps "Because there is no version of
    x==1.0 …" across four, and the last of them is the word "unsatisfiable."
    """
    plain = _ANSI.sub("", completed.stderr or "").translate(_GLYPHS)
    said = " ".join(
        word
        for line in plain.splitlines()
        if not line.strip().startswith(("help:", "hint:"))
        for word in line.split()
    )
    if not said:
        return f"uv exited {completed.returncode}"
    return said if len(said) <= 300 else f"{said[:297]}..."


def probe(package: str, version: str, *, run=subprocess.run) -> str | None:
    """None when `uv` here can resolve and install ``version``, else why it cannot."""
    try:
        completed = run(
            command(package, version),
            capture_output=True,
            # `text=True` alone decodes with the *locale* encoding, and uv writes UTF-8: on a
            # runner whose locale is not UTF-8 the complaint arrives as mojibake, which is how
            # the box-drawing below survives being stripped. `replace` because a garbled byte
            # is not worth raising over inside a retry loop.
            encoding="utf-8",
            errors="replace",
            timeout=ATTEMPT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"uv did not answer within {ATTEMPT_TIMEOUT_SECONDS}s"
    except OSError as error:
        return f"could not run uv: {error}"
    return None if completed.returncode == 0 else why_not(completed)


def await_version(
    package: str,
    version: str,
    *,
    attempt=probe,
    now=time.monotonic,
    sleep=time.sleep,
) -> int:
    """Poll until `uv` can take ``version``; 0 when it can, 1 when the deadline passes."""
    deadline = now() + DEADLINE_SECONDS
    while True:
        reason = attempt(package, version)
        if reason is None:
            print(f"uv here resolves {package} {version}; running the sample")
            return 0
        if now() >= deadline:
            print(
                f"::error::uv on this runner still cannot resolve {package} {version} after "
                f"{DEADLINE_SECONDS}s ({reason}); the sample would resolve an older release "
                "and measure the wrong thing",
                file=sys.stderr,
            )
            return 1
        print(f"not here yet: {reason}; retrying in {INTERVAL_SECONDS}s")
        sleep(INTERVAL_SECONDS)


def main(argv: list[str]) -> int:
    """Wait for this runner's uv to see the published version before a sample resolves."""
    if len(argv) != 3:
        print(f"usage: {argv[0]} <package> <version>", file=sys.stderr)
        return 2
    return await_version(argv[1], argv[2])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
