"""Read PyPI's index documents, retrying the replies that are not answers.

A 404 is an answer — that version is not there — and so is every other 4xx. A reset, a timeout
or a 5xx is the index having a moment, and the gates that read it are required checks, so one
of those must not decide a pull request. They are retried with a widening pause; anything else
comes back, or is raised, at once.

An index still unreachable after the retries raises `IndexUnreachable`, and `run_check` turns
that into a workflow annotation. A gate has two reds — the index could not be asked, and the
thing it measures is wrong — and a maintainer has to tell them apart from the checks page.

Passing when the index cannot be reached is not on offer. These checks exist to prove a range
or a floor resolves against what is published, and one that passed without asking would let an
unresolvable floor through. `check_doc_trackers.py` skips instead, and its findings are prose.
"""

from __future__ import annotations

import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable

#: Tries per document, and the first pause between them; each later pause doubles. Three
#: attempts and three seconds of waiting is sized against a check that reads dozens of
#: documents in one run — long enough for a reset or a 503 to pass, short of a job timeout.
ATTEMPTS = 3
FIRST_PAUSE_SECONDS = 1.0

_TIMEOUT_SECONDS = 30

#: The replies that are the index having a moment rather than answering. One reset reaches here
#: three ways — wrapped in `URLError` when it lands on the connect, bare when it lands on the
#: body read, and as a short body when the close was clean — so all three shapes are named.
_TRANSIENT = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead)


class IndexUnreachable(Exception):
    """PyPI did not answer. Never a verdict on a version — the question was not put."""


def read_json(
    url: str,
    *,
    accept: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict | None:
    """The JSON document at ``url``, or None on a 404.

    Raises ``IndexUnreachable`` when every attempt met a transient failure, and the underlying
    ``HTTPError`` for any other definitive one. ``sleep`` is injected so a test can pin the
    retries without waiting for them.
    """
    request = urllib.request.Request(url, headers={"Accept": accept} if accept else {})
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                return json.load(response)
        # `HTTPError` is a `URLError`, so this clause has to come first or a 404 would be
        # retried and then reported as an index nobody could reach.
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code < 500:
                raise
            reason = error
        except _TRANSIENT as error:
            reason = error
        if attempt == ATTEMPTS:
            raise IndexUnreachable(
                f"pypi.org did not answer {url} in {ATTEMPTS} attempts ({reason}). The index was "
                "unreachable, so this check could not finish — this is not a verdict on any "
                "version."
            ) from reason
        sleep(FIRST_PAUSE_SECONDS * 2 ** (attempt - 1))


def run_check(main: Callable[[list[str]], int], argv: list[str]) -> int:
    """Run a check's ``main``, reporting an unreachable index as an annotation rather than a trace.

    For the checks that gate. A detector that gates nothing must not colour a run over an index
    it could not reach, which is why `check_release_train_drained.py` calls its ``main`` itself.
    """
    try:
        return main(argv)
    except IndexUnreachable as unreachable:
        print(f"::error::{unreachable}", file=sys.stderr)
        return 1
