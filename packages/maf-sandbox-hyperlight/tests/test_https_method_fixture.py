"""The method probe must measure body bytes and distinguish denial from transport failure."""

from __future__ import annotations

import asyncio
import io
import json
from contextlib import closing
from email.parser import BytesParser
from http.client import HTTPConnection
from types import SimpleNamespace

import pytest
from https_method_fixture import RecordingOrigin, read_body
from https_method_guest import RawHttpSubject


@pytest.mark.parametrize("chunked", [False, True])
def test_origin_records_get_body_with_both_framings(chunked):
    payload = b"synthetic-" + bytes(range(256)) * 40
    with RecordingOrigin() as origin:
        path = origin.prefix + "body?query=retained"
        with closing(
            HTTPConnection("127.0.0.1", origin.server.server_port, timeout=3)
        ) as connection:
            body = iter((payload[:7000], payload[7000:])) if chunked else payload
            connection.request("GET", path, body=body, encode_chunked=chunked)
            response = connection.getresponse()
            assert response.status == 200
            assert response.read() == origin.proof
        [record] = origin.matching(path)
        assert record.method == "GET" and record.body == payload
        assert record.transfer_encoding == ("chunked" if chunked else None)
        assert record.content_length == (None if chunked else str(len(payload)))


def test_origin_accepts_all_supported_verbs_but_never_serves_other_paths():
    with RecordingOrigin() as origin:
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with closing(
                HTTPConnection("127.0.0.1", origin.server.server_port, timeout=3)
            ) as connection:
                path = origin.prefix + method
                connection.request(method, path)
                response = connection.getresponse()
                assert response.status == 200
                assert response.read() == (b"" if method == "HEAD" else origin.proof)
            assert [r.method for r in origin.matching(path)] == [method]
        with closing(
            HTTPConnection("127.0.0.1", origin.server.server_port, timeout=3)
        ) as connection:
            connection.request("GET", "/unrelated")
            response = connection.getresponse()
            assert response.status == 404
            response.read()
        assert not origin.matching("/unrelated")


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        (b"Content-Length: 3\r\n", b"ab"),
        (b"Content-Length: 65537\r\n", b""),
        (b"Content-Length: -1\r\n", b""),
        (b"Content-Length: 0\r\nContent-Length: 3\r\n", b"abc"),
        (b"Transfer-Encoding: gzip\r\n", b""),
        (b"Transfer-Encoding: chunked\r\nContent-Length: 0\r\n", b"0\r\n\r\n"),
        (b"Transfer-Encoding: chunked\r\n", b"3\r\nab"),
        (b"Transfer-Encoding: chunked\r\n", b"10001\r\n"),
        (b"Transfer-Encoding: chunked\r\n", b"3\r\nabcXX0\r\n\r\n"),
        (b"Transfer-Encoding: chunked\r\n", b"3\r\nabc\r\n0\r\n"),
    ],
)
def test_incomplete_ambiguous_and_unbounded_bodies_are_not_recorded_as_empty(headers, body):
    handler = SimpleNamespace(headers=BytesParser().parsebytes(headers), rfile=io.BytesIO(body))
    with pytest.raises(ValueError):
        read_body(handler)


def test_chunk_extensions_and_trailers_are_consumed():
    handler = SimpleNamespace(
        headers=BytesParser().parsebytes(b"Transfer-Encoding: chunked\r\n"),
        rfile=io.BytesIO(b"3;fixture=yes\r\nabc\r\n2\r\nde\r\n0\r\nX-Proof: value\r\n\r\n"),
    )
    assert read_body(handler) == b"abcde"
    assert handler.rfile.read() == b""


@pytest.mark.parametrize(
    "error", ["ErrorCode_TlsProtocolError()", "ErrorCode_ConnectionRefused()", "unexpected"]
)
def test_transport_failures_cannot_pass_as_method_denials(error):
    class BrokenTransport:
        async def run_code(self, code, *, timeout):
            return SimpleNamespace(exit_code=0, stdout=json.dumps({"error": error}), stderr="")

    subject = RawHttpSubject(BrokenTransport(), frozenset())
    with pytest.raises(AssertionError, match="without policy denial"):
        asyncio.run(subject.http_reaches("POST", "https://example.invalid/", timeout=1))
