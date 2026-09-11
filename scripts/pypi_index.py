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

The version helpers those checks share live here too — reading a distribution's published
versions and comparing one against a `<ceiling` bound. They were `check_release_order.py`'s
until that check was removed, and the readers they are built on are here.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

#: Tries per document, and the first pause between them; each later pause doubles. Three
#: attempts and three seconds of waiting is sized against a check that reads dozens of
#: documents in one run — long enough for a reset or a 503 to pass, short of a job timeout.
ATTEMPTS = 3
FIRST_PAUSE_SECONDS = 1.0

_TIMEOUT_SECONDS = 30

_SIMPLE_ACCEPT = "application/vnd.pypi.simple.v1+json"

_PYPI_SIMPLE = "https://pypi.org/simple/"
_INDEX_VARIABLE = "UV_INDEX"
_DEFAULT_INDEX_VARIABLE = "UV_DEFAULT_INDEX"
_STRATEGY_VARIABLE = "UV_INDEX_STRATEGY"

#: The one strategy under which ``uv`` prefers the best version across indexes. ``first-index``
#: resolves only what the first index carrying the distribution offers, and
#: ``unsafe-first-match`` exhausts that index before reaching the next — an order this cannot
#: reproduce, because it turns on a requirement only the caller holds. Reading the first index
#: alone under it reports fewer versions than uv would, never more.
_SEARCHES_EVERY_INDEX = frozenset({"unsafe-best-match"})

#: An index may be given as ``<name>=<url>``. The scheme lookahead keeps a query's own ``=`` out
#: of it.
_NAMED_INDEX = re.compile(r"^[A-Za-z0-9._-]+=(?=[A-Za-z][A-Za-z0-9+.-]*://)")

#: What `version` can order and every ceiling in this repository is written as.
_DOTTED_RELEASE = re.compile(r"^\d+(\.\d+)*$")

#: The replies that are the index having a moment rather than answering. One reset reaches here
#: three ways — wrapped in `URLError` when it lands on the connect, bare when it lands on the
#: body read, and as a short body when the close was clean — so all three shapes are named.
_TRANSIENT = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead)


class IndexUnreachable(Exception):
    """PyPI did not answer. Never a verdict on a version — the question was not put."""


def redacted(url: str) -> str:
    """``url`` without the parts a private index authenticates with.

    An annotation reaches the run log, which is readable by anyone who can read the repository
    and is never masked, so a token in an index URL must not travel in one. Scheme, host, port
    and path stay: they are what a reader needs to tell which index did not answer.
    """
    parsed = urllib.parse.urlsplit(url)
    if not (parsed.username or parsed.password or parsed.query):
        return url
    # Kept verbatim rather than rebuilt from `hostname` and `port`: that pair drops the brackets
    # an IPv6 host is written with, and the result is not a URL any more.
    netloc = parsed.netloc.rpartition("@")[2]
    if parsed.username or parsed.password:
        netloc = f"***@{netloc}"
    return urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, "***" if parsed.query else "", parsed.fragment)
    )


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
                f"the index did not answer {redacted(url)} in {ATTEMPTS} attempts ({reason}). The "
                "index was unreachable, so this check could not finish — this is not a verdict "
                "on any version."
            ) from reason
        sleep(FIRST_PAUSE_SECONDS * 2 ** (attempt - 1))


def version(text: str) -> tuple[int, ...]:
    """The dotted release as a tuple of ints."""
    return tuple(int(part) for part in text.split("."))


def admits(version: tuple[int, ...], ceiling: tuple[int, ...]) -> bool:
    """Whether ``version`` is below the ``<ceiling`` bound, comparing at equal width."""
    width = max(len(version), len(ceiling))
    padded = version + (0,) * (width - len(version))
    return padded < ceiling + (0,) * (width - len(ceiling))


def index_urls() -> tuple[str, ...]:
    """The simple-index bases to read, in the order ``uv`` searches them.

    ``UV_INDEX`` before ``UV_DEFAULT_INDEX``, which is uv's precedence, and PyPI when neither
    names one — read from uv's own variables so a check measures the set the install it gates
    resolves from. An entry may carry uv's optional ``<name>=`` prefix, which is not part of the
    URL.
    """
    named = [_url_of(entry) for entry in os.environ.get(_INDEX_VARIABLE, "").split()]
    default = _url_of(os.environ.get(_DEFAULT_INDEX_VARIABLE, "").strip()) or _PYPI_SIMPLE
    seen: dict[str, None] = {}
    for url in (*named, default):
        if url:
            seen[url] = None
    return tuple(seen)


def _url_of(entry: str) -> str:
    """One index entry as a URL, dropping the name uv lets it be given under.

    The trailing slash is settled on the *path*. Appended to the whole string it lands past a
    query the index carries, where it is not part of any path and the join below would put a
    distribution name inside the query.
    """
    url = _NAMED_INDEX.sub("", entry)
    parsed = urllib.parse.urlsplit(url)
    if not parsed.scheme:
        return ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/") + "/", parsed.query, parsed.fragment)
    )


def package_url(index: str, distribution: str) -> str:
    """``distribution``'s simple document under ``index``, joined onto the path."""
    parsed = urllib.parse.urlsplit(index)
    path = f"{parsed.path.rstrip('/')}/{distribution}/"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def fetch_version_document(distribution: str, released: str) -> dict | None:
    """One version's legacy JSON document, from whichever index carries it.

    Warehouse serves ``/pypi/<name>/<version>/json`` beside its simple index, so the host comes
    from the same variables the simple lookup reads, tried in the same order. Asked of pypi.org
    regardless, a version only a rehearsal index carries answers 404, and a caller reads that as
    "no such version" and drops it — leaving the gate to report exactly the shortfall the
    rehearsal was arranged to disprove.
    """
    for index in index_urls():
        parsed = urllib.parse.urlsplit(index)
        document = urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, f"/pypi/{distribution}/{released}/json", "", "")
        )
        payload = read_json(document)
        if payload is not None:
            return payload
    return None


def fetch_simple(distribution: str) -> dict | None:
    """``distribution``'s PEP 691 simple document, or None if no index searched has it.

    The simple index is fresher than the CDN-cached top-level JSON document, and it is what
    ``uv`` resolves from. A 404 means never released *there*; `read_json` decides the rest.

    How many indexes count is ``UV_INDEX_STRATEGY``'s answer rather than one made here. uv's
    default stops at the first index carrying the distribution and resolves only what that one
    offers; its two ``unsafe-`` strategies look at the rest. Merging regardless would let a
    check admit a version the install it gates cannot reach.
    """
    merge = os.environ.get(_STRATEGY_VARIABLE, "").strip() in _SEARCHES_EVERY_INDEX
    payloads: list[dict] = []
    for url in index_urls():
        payload = read_json(package_url(url, distribution), accept=_SIMPLE_ACCEPT)
        if payload is None:
            continue
        if not merge:
            return payload
        payloads.append(payload)
    if not payloads:
        return None
    if len(payloads) == 1:
        return payloads[0]
    merged = dict(payloads[0])
    merged["versions"] = sorted({v for payload in payloads for v in payload.get("versions", ())})
    merged["files"] = [file for payload in payloads for file in payload.get("files", ())]
    return merged


def fetch_published_versions(distribution: str) -> list[str] | None:
    """The published dotted releases of ``distribution``, newest-first, or None if never released.

    The ``versions`` array is standardized (PEP 700) but its order carries no meaning, so it is
    sorted here; it names versions only, and whether a release was yanked or what its
    ``requires_dist`` excludes lives in the per-version document, which a caller that cares
    must fetch.

    **Anything that is not a dotted release is left out**, and an index is free to carry one —
    TestPyPI holds a `maf-sandbox 0.1.0.post1` from years ago. `version` orders dotted releases
    and raises on the rest, and every ceiling a caller compares against is written as one, so
    the alternatives were a PEP 440 ordering nothing here would use, or a `ValueError` that
    takes a check out over an artifact it has nothing to say about. Nothing this repository
    publishes is anything else: `check_rehearsal_version` refuses to rehearse one and
    release-please cuts none.
    """
    payload = fetch_simple(distribution)
    if payload is None:
        return None
    releases = [text for text in payload["versions"] if _DOTTED_RELEASE.match(text)]
    return sorted(releases, key=version, reverse=True)


def newest_upload(payload: dict) -> str | None:
    """The most recent ``upload-time`` across a simple document's files, or None if it has none.

    PEP 700 made the field mandatory for new uploads and optional for old ones, so a
    distribution whose files all predate it answers None rather than a wrong minimum.
    """
    stamps = [file["upload-time"] for file in payload["files"] if file.get("upload-time")]
    return max(stamps) if stamps else None


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
