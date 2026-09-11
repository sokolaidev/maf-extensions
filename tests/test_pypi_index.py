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
    monkeypatch.delenv("UV_INDEX", raising=False)
    monkeypatch.delenv("UV_DEFAULT_INDEX", raising=False)
    monkeypatch.delenv("UV_INDEX_STRATEGY", raising=False)


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

    def test_a_4xx_that_is_not_404_raises_at_once(self, monkeypatch):
        fake = _Index(_http_error(403))
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
        assert index.index_urls()[0].startswith("https://mirror.example/simple?token=abc")

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
    """Under uv's two `unsafe-` strategies every index is searched, so every index counts.

    `first-index` is the other half and has its own class below. Which one applies is read from
    `UV_INDEX_STRATEGY` rather than chosen here: a check that merged under a strategy the
    resolver does not would admit versions the install cannot reach.
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

    @pytest.mark.parametrize("strategy", ["", "first-index"])
    def test_the_second_index_is_never_asked(self, monkeypatch: pytest.MonkeyPatch, strategy: str):
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
