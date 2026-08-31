"""The retry every published-index check inherits, and what it refuses to retry.

The rule the checks depend on is the split: a 404 and any other 4xx are answers and come back
at once, while a reset, a timeout and a 5xx are retried. Both halves are pinned here — a retry
that swallowed a 404 would turn "this version is not published" into a pause and then a red,
and a 4xx retried three times is three times the wait for the same refusal.

Nothing here reaches the network or sleeps: ``urlopen`` is mocked and ``sleep`` is injected, so
the pauses are asserted as values rather than waited for.
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
