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

import base64
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

#: PEP 440, as much of it as an index can hand back: epoch, release, pre, post, dev and local.
_PEP440 = re.compile(
    r"^\s*v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_letter>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_number>\d*))?"
    r"(?:-(?P<post_bare>\d+)|[-_.]?(?P<post_letter>post|rev|r)[-_.]?(?P<post_number>\d*))?"
    r"(?:[-_.]?dev(?P<dev_number>\d*))?"
    r"(?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?\s*$",
    re.IGNORECASE,
)

#: Pre-release letters in PEP 440's order, with their spellings folded onto one another.
_PRE_ORDER = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}

#: How ``uv`` spells an index name inside an environment variable.
_NOT_IN_A_VARIABLE = re.compile(r"[^A-Za-z0-9]")

#: The replies that are the index having a moment rather than answering. One reset reaches here
#: three ways — wrapped in `URLError` when it lands on the connect, bare when it lands on the
#: body read, and as a short body when the close was clean — so all three shapes are named.
_TRANSIENT = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead)


class IndexUnreachable(Exception):
    """PyPI did not answer. Never a verdict on a version — the question was not put."""


class _CredentialStaysAtItsOrigin(urllib.request.HTTPRedirectHandler):
    """Drop ``Authorization`` when a redirect leaves the origin it was minted for.

    urllib copies every header across a redirect, so a mirror answering 302 towards another host
    hands that host a private index's credential. Scheme, host and port have to match for it to
    travel; the request is still followed without it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        following = super().redirect_request(req, fp, code, msg, headers, newurl)
        if following is None or _same_origin(req.full_url, following.full_url):
            return following
        for store in (following.headers, following.unredirected_hdrs):
            for key in [name for name in store if name.lower() == "authorization"]:
                del store[key]
        return following


def _same_origin(one: str, other: str) -> bool:
    """Whether two URLs share a scheme, host and port, case-folded as a URL is."""
    first, second = urllib.parse.urlsplit(one), urllib.parse.urlsplit(other)
    return (first.scheme.lower(), first.hostname, first.port) == (
        second.scheme.lower(),
        second.hostname,
        second.port,
    )


#: Installed globally rather than passed at each call site, so no reader has to remember: every
#: `urlopen` below goes through it, and a caller that forgot would leak rather than fail.
_OPENER = urllib.request.build_opener(_CredentialStaysAtItsOrigin)
urllib.request.install_opener(_OPENER)


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
    authorization: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict | None:
    """The JSON document at ``url``, or None on a 404.

    Raises ``IndexUnreachable`` when every attempt met a transient failure, when the index
    refused the request, and the underlying ``HTTPError`` for any other definitive one.
    ``sleep`` is injected so a test can pin the retries without waiting for them.

    A refusal is ``IndexUnreachable`` rather than a trace because it is the same kind of
    outcome: the question was not put. ``uv`` reaches credentials this does not — a keyring and
    a netrc among them — so an index it can open may be one this cannot.
    """
    headers = {}
    if accept:
        headers["Accept"] = accept
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(url, headers=headers)
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
            if error.code in (401, 403):
                raise IndexUnreachable(
                    f"{redacted(url)} refused the request ({error.code}). A named index is "
                    "authenticated from UV_INDEX_<NAME>_USERNAME and UV_INDEX_<NAME>_PASSWORD "
                    "here; a keyring or netrc credential uv would use is not available to this "
                    "check, so the index could not be asked — this is not a verdict on any "
                    "version."
                ) from error
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
    """The release segment as a tuple of ints, which is what a ``<ceiling`` bound is written as.

    A pre-, post-, dev- or local-version answers its release: ``0.38.0.post1`` is ``(0, 38, 0)``,
    and that is what decides whether a range admits it — ``>=0.38.0,<0.39`` does. `sort_key`
    orders two of them; this only places one against a bound.
    """
    match = _PEP440.match(text)
    if match is None:
        raise ValueError(f"{text!r} is not a PEP 440 version")
    return tuple(int(part) for part in match.group("release").split("."))


def sort_key(text: str) -> tuple[object, ...]:
    """PEP 440's ordering: epoch, release, then dev before pre before final before post.

    The absent-pre sentinel carries the rule that a dev release with no pre sorts *below* one
    with a pre, which is the only part of this order that is not simply "nothing sorts last".
    """
    match = _PEP440.match(text)
    if match is None:
        raise ValueError(f"{text!r} is not a PEP 440 version")
    release = tuple(int(part) for part in match.group("release").split("."))
    post_number = match.group("post_bare") or match.group("post_number")
    has_post = bool(match.group("post_letter") or match.group("post_bare"))
    has_dev = match.group("dev_number") is not None
    if match.group("pre_letter"):
        pre = (_PRE_ORDER[match.group("pre_letter").lower()], int(match.group("pre_number") or 0))
    else:
        pre = (-1, 0) if has_dev and not has_post else (len(set(_PRE_ORDER.values())), 0)
    return (
        int(match.group("epoch") or 0),
        release,
        pre,
        (1, int(post_number or 0)) if has_post else (0, 0),
        (0, int(match.group("dev_number") or 0)) if has_dev else (1, 0),
        match.group("local") or "",
    )


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
    return tuple(url for url, _ in configured_indexes())


def configured_indexes() -> tuple[tuple[str, str | None], ...]:
    """Each index base with the ``Authorization`` header ``uv`` would send it, in search order.

    An entry uv admits as ``<name>=<url>`` keeps its name here, because that name is how uv's
    own ``UV_INDEX_<NAME>_USERNAME`` and ``UV_INDEX_<NAME>_PASSWORD`` address it. Dropped, a
    check reads an authenticated index unauthenticated and is refused or sent elsewhere, which
    is the reader and the resolver disagreeing again one layer down.

    Both of uv's credential forms are honoured, and **a named variable beats userinfo embedded
    in the same URL**: uv's own precedence between them is not established here, and a variable
    set beside the index is the more deliberate of the two. A keyring and a netrc are out of
    reach either way; `read_json` says so when an index refuses.

    A bare name with no URL contributes no index, matching uv, which accepts the form and
    resolves from the default index.
    """
    entries = [_named(entry) for entry in os.environ.get(_INDEX_VARIABLE, "").split()]
    name, default, embedded = _named(os.environ.get(_DEFAULT_INDEX_VARIABLE, "").strip())
    entries.append((name, default or _PYPI_SIMPLE, embedded))
    seen: dict[str, str | None] = {}
    for index_name, url, credential in entries:
        if url and url not in seen:
            seen[url] = (_authorization(index_name) if index_name else None) or credential
    return tuple(seen.items())


def _named(entry: str) -> tuple[str, str, str | None]:
    """One index entry as its uv name, its URL without userinfo, and what that userinfo means.

    ``urllib`` does not read userinfo as authentication: it hands ``user:pass@host`` to the
    resolver, the lookup fails, and the failure is a ``URLError`` this module retries and then
    reports as an index nobody could reach. uv admits credentials embedded in an index URL, so
    they are converted here rather than passed on to fail as an outage.

    The trailing slash is settled on the *path*. Appended to the whole string it lands past a
    query the index carries, where it is not part of any path and the join in `package_url`
    would put a distribution name inside the query.
    """
    match = _NAMED_INDEX.match(entry)
    name = entry[: match.end() - 1] if match else ""
    parsed = urllib.parse.urlsplit(entry[match.end() :] if match else entry)
    if not parsed.scheme:
        return name, "", None
    url = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc.rpartition("@")[2],
            parsed.path.rstrip("/") + "/",
            parsed.query,
            parsed.fragment,
        )
    )
    # Percent-decoded, because `urlsplit` does not: a password written `p%40ss` in a URL is
    # `p@ss` to uv, and the two authenticate as different credentials.
    return name, url, _basic(_decoded(parsed.username), _decoded(parsed.password))


def _decoded(part: str | None) -> str | None:
    """One userinfo component as its characters rather than its percent-encoding."""
    return None if part is None else urllib.parse.unquote(part)


def _authorization(name: str) -> str | None:
    """The Basic header uv composes for a named index, or None when neither variable is set."""
    key = _NOT_IN_A_VARIABLE.sub("_", name).upper()
    return _basic(
        os.environ.get(f"{_INDEX_VARIABLE}_{key}_USERNAME"),
        os.environ.get(f"{_INDEX_VARIABLE}_{key}_PASSWORD"),
    )


def _basic(user: str | None, secret: str | None) -> str | None:
    """A Basic header for one credential pair, or None when neither half is set."""
    if user is None and secret is None:
        return None
    return "Basic " + base64.b64encode(f"{user or ''}:{secret or ''}".encode()).decode()


def package_url(index: str, distribution: str) -> str:
    """``distribution``'s simple document under ``index``, joined onto the path."""
    parsed = urllib.parse.urlsplit(index)
    path = f"{parsed.path.rstrip('/')}/{distribution}/"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def version_document_url(index: str, distribution: str, released: str) -> str:
    """Where a simple index at ``index`` serves one version's legacy JSON document.

    Warehouse puts it beside the simple index, so the base's own path and query are kept and
    only the ``simple`` segment is exchanged for ``pypi``. Rebuilt from the host alone instead,
    a path-scoped mirror is asked at a prefix it does not serve and a query-authenticated one is
    asked without its credential — and both answer as though the version were not there.
    """
    parsed = urllib.parse.urlsplit(index)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if segments and segments[-1] == "simple":
        segments = segments[:-1]
    path = "/" + "/".join([*segments, "pypi", distribution, released, "json"])
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def fetch_version_document(distribution: str, released: str) -> dict | None:
    """One version's legacy JSON document, from whichever index carries it.

    The same indexes the simple lookup reads, in the same order and with the same credentials.
    An index that is not Warehouse serves no such document and answers 404, which falls through
    to the next rather than ending the search; there is no per-version ``requires_dist`` in the
    simple API to fall back to.
    """
    for index, authorization in configured_indexes():
        payload = read_json(
            version_document_url(index, distribution, released), authorization=authorization
        )
        if payload is not None:
            return payload
    return None


def fetch_simple(distribution: str) -> dict | None:
    """``distribution``'s PEP 691 simple document, or None if no index searched has it.

    The simple index is fresher than the CDN-cached top-level JSON document, and it is what
    ``uv`` resolves from. A 404 means never released *there*; `read_json` decides the rest.

    How many indexes count is ``UV_INDEX_STRATEGY``'s answer rather than one made here, and
    **only ``unsafe-best-match`` merges**: uv's default resolves what the first index carrying
    the distribution offers, and ``unsafe-first-match`` prefers that index's versions without
    stopping at them — an order that turns on a requirement this layer does not hold, so it is
    approximated by the first index alone. That reports fewer versions than uv would consider,
    never one it would refuse, which is the direction a release gate should be wrong in.
    """
    merge = os.environ.get(_STRATEGY_VARIABLE, "").strip() in _SEARCHES_EVERY_INDEX
    payloads: list[dict] = []
    for url, authorization in configured_indexes():
        payload = read_json(
            package_url(url, distribution), accept=_SIMPLE_ACCEPT, authorization=authorization
        )
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

    A pre-, post-, dev- or local-version is carried like any other. An index is free to hold
    one, a range this repository writes can admit it — ``0.38.0.post1`` satisfies
    ``>=0.38.0,<0.39`` — and a resolver may select it, so a reader that dropped it would gate
    on a different set than the install it guards.
    """
    payload = fetch_simple(distribution)
    if payload is None:
        return None
    return sorted(payload["versions"], key=sort_key, reverse=True)


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
