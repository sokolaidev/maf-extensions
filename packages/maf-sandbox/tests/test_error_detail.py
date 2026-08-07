"""Tests for `maf_sandbox.error_detail`.

Moved here from the bicep kind, where it started life as `_tool.py`'s private
``_error_detail`` — the ACA backend has the identical gap (a bare ``%s`` of the exception,
dropping the azure-core response body) and duck-typing means neither caller has to import
Azure to use it.
"""

from __future__ import annotations

from maf_sandbox import error_detail


class TestErrorDetail:
    def test_the_bare_type_and_message_are_always_present(self):
        assert error_detail(ValueError("boom")) == "ValueError: boom"

    def test_status_code_is_appended_when_present(self):
        class _Err(Exception):
            status_code = 400

        assert error_detail(_Err("bad")) == "_Err: bad | status=400"

    def test_status_code_is_omitted_when_absent(self):
        assert "status=" not in error_detail(RuntimeError("x"))

    def test_status_code_zero_is_still_reported(self):
        """`getattr(..., None) is not None` — a falsy-but-real status must not be dropped."""

        class _Err(Exception):
            status_code = 0

        assert "status=0" in error_detail(_Err("x"))

    def test_response_body_is_appended_when_present(self):
        class _Response:
            @staticmethod
            def text() -> str:
                return '{"error":"principal lacks a role"}'

        class _Err(Exception):
            status_code = 403
            response = _Response()

        detail = error_detail(_Err("forbidden"))
        assert "status=403" in detail
        assert "principal lacks a role" in detail
        assert detail.startswith("_Err: forbidden | status=403 | body=")

    def test_response_is_omitted_when_absent(self):
        assert "body=" not in error_detail(RuntimeError("x"))

    def test_an_empty_response_body_is_omitted(self):
        class _Response:
            @staticmethod
            def text() -> str:
                return ""

        class _Err(Exception):
            response = _Response()

        assert "body=" not in error_detail(_Err("x"))

    def test_response_text_raising_does_not_break_the_diagnostic(self):
        """Diagnostics must never raise — a body that cannot be read is simply omitted."""

        class _Response:
            @staticmethod
            def text() -> str:
                raise RuntimeError("stream already consumed")

        class _Err(Exception):
            status_code = 500
            response = _Response()

        detail = error_detail(_Err("boom"))
        assert detail == "_Err: boom | status=500"

    def test_the_body_is_truncated_at_600_characters(self):
        class _Response:
            @staticmethod
            def text() -> str:
                return "x" * 1000

        class _Err(Exception):
            response = _Response()

        detail = error_detail(_Err("x"))
        body = detail.split("body=", 1)[1]
        assert len(body) == 600

    def test_the_parts_are_joined_with_pipe(self):
        class _Response:
            @staticmethod
            def text() -> str:
                return "detail"

        class _Err(Exception):
            status_code = 409
            response = _Response()

        assert error_detail(_Err("conflict")) == "_Err: conflict | status=409 | body=detail"
