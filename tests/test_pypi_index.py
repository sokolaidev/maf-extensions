"""The retry every published-index check inherits, and what it refuses to retry.

The rule the checks depend on is the split: a 404 and any other 4xx are answers and come back
at once, while a reset, a timeout and a 5xx are retried. Both halves are pinned here — a retry
that swallowed a 404 would turn "this version is not published" into a pause and then a red,
and a 4xx retried three times is three times the wait for the same refusal.

Nothing here reaches the network or sleeps: ``urlopen`` is mocked and ``sleep`` is injected, so
the pauses are asserted as values rather than waited for.

The version helpers the same checks share are pinned here too, sorting being the one that goes
wrong quietly: a lexical order puts 0.9.0 above 0.10.0 and every caller reads the wrong newest.
"""

from __future__ import annotations

import email.message
import http.client
import importlib.util
import io
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("pypi_index", _SCRIPTS / "pypi_index.py")
assert _spec and _spec.loader
index = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(index)

_URL = "https://pypi.org/pypi/maf-sandbox/0.16.0/json"


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(_URL, code, "err", email.message.Message(), io.BytesIO(b""))


class _Response:
    """The slice of an HTTP response ``json.load`` reads: a ``read()`` returning JSON bytes."""

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


class _Index:
    """A fake PyPI that answers each call with the next item of ``replies``.

    An exception instance is raised, anything else is served as a JSON body. Records every
    ``Request`` it was handed, so a test can count the attempts and read the headers.
    """

    def __init__(self, *replies: object) -> None:
        self._replies = list(replies)
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, timeout: int | None = None) -> _Response:
        self.requests.append(request)
        reply = self._replies.pop(0) if self._replies else self._replies
        if isinstance(reply, BaseException):
            raise reply
        return _Response(reply)


def _install(monkeypatch: pytest.MonkeyPatch, fake: _Index) -> list[float]:
    """Point ``urlopen`` at ``fake``; answer the list the injected sleep will record into."""
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return []


@pytest.fixture(autouse=True)
def _one_index_unless_a_test_says_otherwise(monkeypatch: pytest.MonkeyPatch):
    """A contributor's own index settings would change how many documents each read fetches."""
    for name in [key for key in os.environ if key.startswith("UV_INDEX")]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("UV_DEFAULT_INDEX", raising=False)


class TestADefinitiveReplyIsNotRetried:
    def test_a_document_comes_back_on_the_first_attempt(self, monkeypatch):
        fake = _Index({"versions": ["0.16.0"]})
        pauses = _install(monkeypatch, fake)
        assert index.read_json(_URL, sleep=pauses.append) == {"versions": ["0.16.0"]}
        assert len(fake.requests) == 1
        assert pauses == []

    def test_a_404_answers_none_without_a_second_attempt(self, monkeypatch):
        fake = _Index(_http_error(404), _http_error(404), _http_error(404))
        pauses = _install(monkeypatch, fake)
        assert index.read_json(_URL, sleep=pauses.append) is None
        assert len(fake.requests) == 1

    @pytest.mark.parametrize("code", [400, 410, 422])
    def test_a_4xx_that_is_neither_404_nor_a_refusal_raises_at_once(self, monkeypatch, code):
        fake = _Index(_http_error(code))
        pauses = _install(monkeypatch, fake)
        with pytest.raises(urllib.error.HTTPError):
            index.read_json(_URL, sleep=pauses.append)
        assert len(fake.requests) == 1

    def test_the_accept_header_rides_along_only_when_asked_for(self, monkeypatch):
        fake = _Index({"versions": []}, {"versions": []})
        pauses = _install(monkeypatch, fake)
        index.read_json(_URL, accept="application/vnd.pypi.simple.v1+json", sleep=pauses.append)
        index.read_json(_URL, sleep=pauses.append)
        assert fake.requests[0].get_header("Accept") == "application/vnd.pypi.simple.v1+json"
        assert fake.requests[1].get_header("Accept") is None


class TestATransientReplyIsRetried:
    @pytest.mark.parametrize(
        "transient",
        [
            urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")),
            ConnectionResetError(104, "Connection reset by peer"),
            TimeoutError("timed out"),
            http.client.IncompleteRead(b"{"),
            _http_error(503),
        ],
        ids=["urlerror", "bare-reset", "timeout", "short-body", "503"],
    )
    def test_one_of_them_costs_a_pause_and_not_the_run(self, monkeypatch, transient):
        fake = _Index(transient, {"versions": ["0.16.0"]})
        pauses = _install(monkeypatch, fake)
        assert index.read_json(_URL, sleep=pauses.append) == {"versions": ["0.16.0"]}
        assert len(fake.requests) == 2

    def test_the_pause_widens_between_attempts(self, monkeypatch):
        fake = _Index(_http_error(503), _http_error(503), {"versions": []})
        pauses = _install(monkeypatch, fake)
        index.read_json(_URL, sleep=pauses.append)
        assert pauses == [1.0, 2.0]

    def test_the_attempts_are_bounded(self, monkeypatch):
        fake = _Index(*[_http_error(503)] * 10)
        pauses = _install(monkeypatch, fake)
        with pytest.raises(index.IndexUnreachable):
            index.read_json(_URL, sleep=pauses.append)
        assert len(fake.requests) == index.ATTEMPTS


class TestWhatAnExhaustedRetryReports:
    """The message is the whole point: it has to read as the index, not as a version."""

    def test_it_names_the_document_and_says_it_is_not_a_verdict(self, monkeypatch):
        reset = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
        pauses = _install(monkeypatch, _Index(reset, reset, reset))
        with pytest.raises(index.IndexUnreachable) as raised:
            index.read_json(_URL, sleep=pauses.append)
        said = str(raised.value)
        assert _URL in said
        assert "not a verdict" in said
        assert "Connection reset by peer" in said
        assert raised.value.__cause__ is reset

    def test_it_claims_only_that_the_check_stopped_not_that_nothing_ran(self, monkeypatch):
        """A multi-document check reaches here with work already reported.

        `check_samples_against_declared_core` prints each group's `ok`/`FAIL` lines as it goes,
        so an unreachable index in the fifth group leaves four groups' results on the log. An
        annotation saying nothing was checked contradicts what the reader can already see, and
        being believed at a glance is this message's whole job.
        """
        pauses = _install(monkeypatch, _Index(*[_http_error(503)] * index.ATTEMPTS))
        with pytest.raises(index.IndexUnreachable) as raised:
            index.read_json(_URL, sleep=pauses.append)
        said = str(raised.value)
        assert "could not finish" in said
        assert "nothing was checked" not in said

    def test_the_message_is_one_line_so_the_annotation_survives(self, monkeypatch):
        pauses = _install(monkeypatch, _Index(*[_http_error(503)] * 3))
        with pytest.raises(index.IndexUnreachable) as raised:
            index.read_json(_URL, sleep=pauses.append)
        assert "\n" not in str(raised.value)


class TestRunCheck:
    def test_it_passes_a_checks_own_exit_code_through(self):
        def main(argv: list[str]) -> int:
            return len(argv)

        assert index.run_check(main, ["check", "--flag"]) == 2

    def test_an_unreachable_index_becomes_one_annotation_and_exit_one(self, capsys):
        def main(_argv: list[str]) -> int:
            raise index.IndexUnreachable("pypi.org did not answer")

        assert index.run_check(main, ["check"]) == 1
        annotation = capsys.readouterr().err
        assert annotation == "::error::pypi.org did not answer\n"

    def test_it_does_not_catch_what_a_check_means_to_report(self):
        def main(_argv: list[str]) -> int:
            raise SystemExit("this floor resolves to nothing")

        with pytest.raises(SystemExit):
            index.run_check(main, ["check"])


class TestPublishedVersionsAreSortedSemantically:
    """Newest-first, by numeric value, never lexically."""

    def test_0_10_0_sorts_after_0_9_0(self, monkeypatch: pytest.MonkeyPatch):
        _install(monkeypatch, _Index({"versions": ["0.6.0", "0.10.0", "0.9.0"]}))
        assert index.fetch_published_versions("maf-sandbox-bicep") == ["0.10.0", "0.9.0", "0.6.0"]

    def test_an_unsorted_multi_part_order_is_preserved_by_value(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _install(monkeypatch, _Index({"versions": ["1.2.4", "1.2.10", "1.3.0", "1.2.3"]}))
        assert index.fetch_published_versions("maf-sandbox-bicep") == [
            "1.3.0",
            "1.2.10",
            "1.2.4",
            "1.2.3",
        ]

    def test_a_distribution_that_was_never_released_answers_none(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """None, not an empty list: a caller has to tell "no versions" from "no package"."""
        _install(monkeypatch, _Index(_http_error(404)))
        assert index.fetch_published_versions("maf-sandbox-nothing") is None


class TestAVersionsMetadataComesFromTheIndexThatCarriesIt:
    """Warehouse serves `/pypi/<name>/<version>/json` beside its simple index, so both follow
    the same variables: the same indexes, the same order, the same credentials, and the base's
    own path and query kept.

    A version one index holds and another does not is the ordinary case, not a corner: a caller
    reads a 404 as "no such version", so asking the wrong index answers about a different one.
    """

    def test_the_rehearsal_index_is_asked_first_and_answers(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("UV_INDEX", "https://test.pypi.org/simple/")
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://pypi.org/simple/")
        fake = _Index({"info": {"requires_dist": ["maf-sandbox>=0.38.0,<0.39"]}})
        _install(monkeypatch, fake)
        payload = index.fetch_version_document("maf-sandbox-deepagents", "0.1.0")
        assert payload is not None
        assert fake.requests[0].full_url == (
            "https://test.pypi.org/pypi/maf-sandbox-deepagents/0.1.0/json"
        )

    def test_an_index_without_the_version_falls_through_to_the_next(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("UV_INDEX", "https://test.pypi.org/simple/")
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://pypi.org/simple/")
        fake = _Index(_http_error(404), {"info": {"requires_dist": []}})
        _install(monkeypatch, fake)
        assert index.fetch_version_document("maf-sandbox", "0.37.0") is not None
        assert [request.full_url for request in fake.requests] == [
            "https://test.pypi.org/pypi/maf-sandbox/0.37.0/json",
            "https://pypi.org/pypi/maf-sandbox/0.37.0/json",
        ]

    def test_a_version_no_index_carries_is_none(self, monkeypatch: pytest.MonkeyPatch):
        _install(monkeypatch, _Index(_http_error(404)))
        assert index.fetch_version_document("maf-sandbox", "99.0.0") is None

    def test_by_default_it_asks_pypi_and_nothing_else(self, monkeypatch: pytest.MonkeyPatch):
        fake = _Index({"info": {}})
        _install(monkeypatch, fake)
        index.fetch_version_document("maf-sandbox", "0.37.0")
        assert [request.full_url for request in fake.requests] == [
            "https://pypi.org/pypi/maf-sandbox/0.37.0/json"
        ]

    @pytest.mark.parametrize(
        ("base", "expected"),
        [
            ("https://pypi.org/simple/", "https://pypi.org/pypi/maf-sandbox/0.38.0/json"),
            (
                "https://test.pypi.org/simple/",
                "https://test.pypi.org/pypi/maf-sandbox/0.38.0/json",
            ),
            (
                "https://mirror.example/repository/simple/",
                "https://mirror.example/repository/pypi/maf-sandbox/0.38.0/json",
            ),
            (
                "https://mirror.example/repository/simple/?token=abc",
                "https://mirror.example/repository/pypi/maf-sandbox/0.38.0/json?token=abc",
            ),
            (
                "https://mirror.example/idx/",
                "https://mirror.example/idx/pypi/maf-sandbox/0.38.0/json",
            ),
        ],
    )
    def test_the_base_keeps_its_own_path_and_query(self, base: str, expected: str):
        """A mirror scoped under a prefix, or authenticating by query, is asked where it lives.

        Rebuilt from the host alone it is asked at a path it does not serve, without the
        credential, and answers as though the version were not published.
        """
        assert index.version_document_url(base, "maf-sandbox", "0.38.0") == expected


class TestAnIndexMayCarryWhatThisRepositoryNeverPublishes:
    """`version` orders dotted releases and raises on the rest, and ceilings are written as one.

    TestPyPI holds `maf-sandbox 0.1.0.post1`, so reading it is not hypothetical: included, it
    reaches the sort and takes the check out with a `ValueError` over an artifact nothing here
    has anything to say about.
    """

    def _carrying(self, monkeypatch: pytest.MonkeyPatch, *versions: str) -> None:
        monkeypatch.setattr(index, "fetch_simple", lambda _name: {"versions": list(versions)})

    def test_the_post_release_testpypi_actually_holds_does_not_stop_the_sort(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._carrying(monkeypatch, "0.1.0", "0.1.0.post1", "0.2.0", "0.33.0", "0.34.0", "0.38.0")
        assert index.fetch_published_versions("maf-sandbox") == [
            "0.38.0",
            "0.34.0",
            "0.33.0",
            "0.2.0",
            "0.1.0",
        ]

    @pytest.mark.parametrize("odd", ["1.0.0rc1", "1.0.0b2", "1.0.0.dev3", "1.0.0+local", "latest"])
    def test_nothing_but_a_dotted_release_comes_back(
        self, monkeypatch: pytest.MonkeyPatch, odd: str
    ):
        self._carrying(monkeypatch, "1.0.0", odd)
        assert index.fetch_published_versions("maf-sandbox") == ["1.0.0"]

    def test_an_index_carrying_only_those_answers_empty_rather_than_none(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Empty is "published, nothing this can order"; None stays "never published"."""
        self._carrying(monkeypatch, "0.1.0.post1")
        assert index.fetch_published_versions("maf-sandbox") == []


class TestWhichIndexesAreRead:
    """The checks read exactly what `uv` would resolve from: its variables, in its order.

    `UV_INDEX` first and `UV_DEFAULT_INDEX` behind it, PyPI when neither names one, and an
    entry's optional `<name>=` prefix is not part of its URL.
    """

    def test_pypi_is_the_only_index_by_default(self):
        assert index.index_urls() == ("https://pypi.org/simple/",)

    @pytest.mark.parametrize("variable", ["UV_INDEX", "UV_DEFAULT_INDEX"])
    def test_the_name_uv_lets_an_index_carry_is_not_part_of_its_url(
        self, monkeypatch: pytest.MonkeyPatch, variable: str
    ):
        """`uv pip install --index corp=https://…` is documented and resolves; taken whole it
        would request `corp=https://mirror.example/simple/maf-sandbox/` and every check fail."""
        monkeypatch.setenv(variable, "corp=https://mirror.example/simple/")
        assert "https://mirror.example/simple/" in index.index_urls()

    def test_a_query_is_not_mistaken_for_a_name(self, monkeypatch: pytest.MonkeyPatch):
        """The name prefix is recognised by the scheme behind it, so an `=` elsewhere is safe."""
        monkeypatch.setenv("UV_INDEX", "https://mirror.example/simple?token=abc")
        assert index.index_urls()[0] == "https://mirror.example/simple/?token=abc"


class TestADistributionIsJoinedOntoThePath:
    """An index may carry a query, and a name concatenated onto the whole URL lands inside it.

    The request then asks for a package nobody named, against a path the index does not serve —
    and on an index that authenticates by query, it puts the name where the credential is.
    """

    @pytest.mark.parametrize(
        "base",
        [
            "https://mirror.example/simple",
            "https://mirror.example/simple/",
            "https://mirror.example/simple///",
        ],
    )
    def test_however_the_base_ends_the_path_gains_one_segment(self, base: str):
        assert (
            index.package_url(base, "maf-sandbox") == "https://mirror.example/simple/maf-sandbox/"
        )

    def test_a_query_rides_behind_the_path_rather_than_swallowing_it(self):
        assert (
            index.package_url("https://mirror.example/simple/?token=abc", "maf-sandbox")
            == "https://mirror.example/simple/maf-sandbox/?token=abc"
        )

    def test_the_document_read_is_the_one_the_path_names(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("UV_INDEX", "https://mirror.example/simple?token=abc")
        fake = _Index({"versions": ["1.0.0"]})
        _install(monkeypatch, fake)
        index.fetch_published_versions("maf-sandbox")
        assert fake.requests[0].full_url == "https://mirror.example/simple/maf-sandbox/?token=abc"

    def test_the_primary_comes_first_and_the_extras_follow(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("UV_INDEX", "https://test.pypi.org/simple/")
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://pypi.org/simple/")
        assert index.index_urls() == ("https://test.pypi.org/simple/", "https://pypi.org/simple/")

    def test_a_missing_trailing_slash_is_not_a_different_index(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("UV_INDEX", "https://test.pypi.org/simple")
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://test.pypi.org/simple/")
        assert index.index_urls() == ("https://test.pypi.org/simple/",)

    def test_several_extras_are_split_the_way_uv_splits_them(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("UV_INDEX", "https://one.example/simple/ https://two.example/simple/")
        assert index.index_urls() == (
            "https://one.example/simple/",
            "https://two.example/simple/",
            "https://pypi.org/simple/",
        )


class TestAVersionOnEitherIndexCounts:
    """Under `unsafe-best-match` uv prefers the best version across indexes, so every one counts.

    The only strategy that merges. `first-index` and `unsafe-first-match` are the other half and
    have their own class below. Which applies is read from `UV_INDEX_STRATEGY` rather than chosen
    here: merging under a strategy the resolver does not would admit versions the install cannot
    reach.
    """

    def _two(self, monkeypatch: pytest.MonkeyPatch, first: object, second: object) -> _Index:
        monkeypatch.setenv("UV_INDEX", "https://test.pypi.org/simple/")
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://pypi.org/simple/")
        monkeypatch.setenv("UV_INDEX_STRATEGY", "unsafe-best-match")
        fake = _Index(first, second)
        _install(monkeypatch, fake)
        return fake

    def test_the_rehearsed_version_is_seen_beside_the_released_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._two(monkeypatch, {"versions": ["0.38.0"]}, {"versions": ["0.36.0", "0.37.0"]})
        assert index.fetch_published_versions("maf-sandbox") == ["0.38.0", "0.37.0", "0.36.0"]

    def test_a_version_on_both_is_named_once(self, monkeypatch: pytest.MonkeyPatch):
        self._two(monkeypatch, {"versions": ["0.37.0"]}, {"versions": ["0.37.0"]})
        assert index.fetch_published_versions("maf-sandbox") == ["0.37.0"]

    def test_an_index_that_never_had_it_does_not_hide_the_one_that_does(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._two(monkeypatch, _http_error(404), {"versions": ["0.37.0"]})
        assert index.fetch_published_versions("maf-sandbox") == ["0.37.0"]

    def test_never_released_anywhere_is_still_none(self, monkeypatch: pytest.MonkeyPatch):
        self._two(monkeypatch, _http_error(404), _http_error(404))
        assert index.fetch_published_versions("maf-sandbox-nothing") is None

    def test_both_indexes_are_asked(self, monkeypatch: pytest.MonkeyPatch):
        fake = self._two(monkeypatch, {"versions": ["0.38.0"]}, {"versions": ["0.37.0"]})
        index.fetch_published_versions("maf-sandbox")
        assert [request.full_url for request in fake.requests] == [
            "https://test.pypi.org/simple/maf-sandbox/",
            "https://pypi.org/simple/maf-sandbox/",
        ]

    def test_the_files_of_both_are_kept_so_upload_times_stay_readable(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        self._two(
            monkeypatch,
            {"versions": ["0.38.0"], "files": [{"upload-time": "2026-09-11T14:00:00Z"}]},
            {"versions": ["0.37.0"], "files": [{"upload-time": "2026-09-10T08:58:00Z"}]},
        )
        payload = index.fetch_simple("maf-sandbox")
        assert payload is not None
        assert index.newest_upload(payload) == "2026-09-11T14:00:00Z"


class TestFirstIndexStopsAtTheFirstMatch:
    """uv's default resolves only what the first index carrying the distribution offers.

    A merged read under it would report versions the install cannot reach, which is the same
    disagreement between check and resolver that naming the indexes exists to end.
    """

    def _two(self, monkeypatch: pytest.MonkeyPatch, first: object, second: object) -> _Index:
        monkeypatch.setenv("UV_INDEX", "https://one.example/simple/")
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://pypi.org/simple/")
        fake = _Index(first, second)
        _install(monkeypatch, fake)
        return fake

    @pytest.mark.parametrize("strategy", ["", "first-index", "unsafe-first-match"])
    def test_the_second_index_is_never_asked(self, monkeypatch: pytest.MonkeyPatch, strategy: str):
        """`unsafe-first-match` belongs here, not with the merge: uv exhausts the first index's
        versions before reaching the next, an order that turns on a requirement this layer does
        not hold. Reading the first index alone reports fewer versions than uv would, never a
        version it would refuse."""
        if strategy:
            monkeypatch.setenv("UV_INDEX_STRATEGY", strategy)
        fake = self._two(monkeypatch, {"versions": ["0.1.0"]}, {"versions": ["9.9.9"]})
        assert index.fetch_published_versions("maf-sandbox") == ["0.1.0"]
        assert [request.full_url for request in fake.requests] == [
            "https://one.example/simple/maf-sandbox/"
        ]

    def test_an_index_without_it_is_passed_over_rather_than_ending_the_search(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """uv stops at the first index that returns a match, not at the first index asked."""
        self._two(monkeypatch, _http_error(404), {"versions": ["0.37.0"]})
        assert index.fetch_published_versions("maf-sandbox") == ["0.37.0"]

    def test_an_unknown_strategy_does_not_widen_the_search(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("UV_INDEX_STRATEGY", "whatever-uv-adds-next")
        self._two(monkeypatch, {"versions": ["0.1.0"]}, {"versions": ["9.9.9"]})
        assert index.fetch_published_versions("maf-sandbox") == ["0.1.0"]


class TestANamedIndexCarriesTheCredentialUvWouldSend:
    """uv addresses a named index's credentials by that name, so the name has to survive parsing.

    Stripped, the check reads an authenticated index unauthenticated: refused, or quietly sent
    to the next index, which is the reader and the resolver disagreeing one layer below where
    naming the index was meant to settle it.
    """

    def test_the_header_is_composed_from_uvs_own_variables(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("UV_INDEX", "corp=https://mirror.example/simple/")
        monkeypatch.setenv("UV_INDEX_CORP_USERNAME", "reader")
        monkeypatch.setenv("UV_INDEX_CORP_PASSWORD", "secret")
        fake = _Index({"versions": ["1.0.0"]})
        _install(monkeypatch, fake)
        index.fetch_published_versions("maf-sandbox")
        assert fake.requests[0].get_header("Authorization") == "Basic cmVhZGVyOnNlY3JldA=="

    def test_a_name_uv_would_spell_differently_is_spelled_uvs_way(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("UV_INDEX", "my-corp.eu=https://mirror.example/simple/")
        monkeypatch.setenv("UV_INDEX_MY_CORP_EU_USERNAME", "reader")
        assert index.configured_indexes()[0][1] is not None

    def test_an_index_with_no_credentials_sends_none(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("UV_INDEX", "corp=https://mirror.example/simple/")
        fake = _Index({"versions": ["1.0.0"]})
        _install(monkeypatch, fake)
        index.fetch_published_versions("maf-sandbox")
        assert fake.requests[0].get_header("Authorization") is None

    def test_an_unnamed_index_is_never_given_another_indexs_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("UV_INDEX", "https://mirror.example/simple/")
        monkeypatch.setenv("UV_INDEX_CORP_USERNAME", "reader")
        assert index.configured_indexes()[0][1] is None

    def test_a_bare_name_contributes_no_index_as_it_does_for_uv(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """`uv pip install --index somename` resolves from the default index without error."""
        monkeypatch.setenv("UV_INDEX", "somename")
        assert index.index_urls() == ("https://pypi.org/simple/",)

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_refusal_says_the_index_could_not_be_asked(self, monkeypatch, code: int):
        """Not a `None` that a caller reads as "no such version", and not a bare traceback."""
        pauses = _install(monkeypatch, _Index(_http_error(code)))
        with pytest.raises(index.IndexUnreachable) as raised:
            index.read_json(_URL, sleep=pauses.append)
        said = str(raised.value)
        assert "refused the request" in said
        assert "not a verdict" in said
        assert pauses == []


class TestAnIndexUrlMayCarryACredential:
    """The annotation reaches the run log, which is readable and never masked."""

    def test_userinfo_and_query_are_replaced(self):
        assert (
            index.redacted("https://token:secret@mirror.example/simple/pkg/?key=abc")
            == "https://***@mirror.example/simple/pkg/?***"
        )

    def test_the_host_port_and_path_survive_so_the_reader_knows_which_index(self):
        assert (
            index.redacted("https://u:p@mirror.example:8443/simple/pkg/")
            == "https://***@mirror.example:8443/simple/pkg/"
        )

    def test_a_url_carrying_neither_is_untouched(self):
        assert index.redacted(_URL) == _URL

    def test_an_ipv6_host_keeps_the_brackets_that_make_it_a_url(self):
        """Rebuilt from `hostname` and `port` the brackets are gone and `https://***@2001:db8::1:8443/`
        is not an address any more — the one line a reader needs in order to act is the one lost."""
        assert (
            index.redacted("https://u:p@[2001:db8::1]:8443/simple/")
            == "https://***@[2001:db8::1]:8443/simple/"
        )

    def test_an_ipv6_host_with_no_credential_is_left_alone(self):
        plain = "https://[2001:db8::1]:8443/simple/"
        assert index.redacted(plain) == plain

    def test_the_unreachable_message_carries_the_redacted_form(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        secret = "https://token:secret@mirror.example/simple/maf-sandbox/"
        pauses = _install(monkeypatch, _Index(*[_http_error(503)] * index.ATTEMPTS))
        with pytest.raises(index.IndexUnreachable) as raised:
            index.read_json(secret, sleep=pauses.append)
        said = str(raised.value)
        assert "secret" not in said
        assert "token" not in said
        assert "mirror.example/simple/maf-sandbox/" in said
