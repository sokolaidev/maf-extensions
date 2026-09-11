"""Keep the ACAS measurement from mistaking damaged framing for exact output."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from maf_sandbox import ExecResult

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from probe_acas_exec_bytes import (  # noqa: E402
    Captured,
    _case_result,
    _count_capture_directories,
    decode_envelope,
)

_VALID = (
    "maf-exec-bytes-v1:begin\n7\nAP9h\n\nmaf-exec-bytes-v1:stderr\nZf4=\n\nmaf-exec-bytes-v1:end\n"
)


def test_each_stream_is_recovered_without_text_decoding():
    assert decode_envelope(_VALID, "", 7) == Captured(b"\x00\xffa", b"e\xfe", 7)


def test_every_truncated_prefix_is_refused():
    for end in range(len(_VALID)):
        with pytest.raises(ValueError):
            decode_envelope(_VALID[:end], "", 7)


@pytest.mark.parametrize(
    "damaged",
    [
        _VALID.replace("AP9h", "AP!h"),
        _VALID.replace("Zf4=", "Zf4"),
        _VALID.replace("\n7\n", "\n0\n"),
        _VALID.replace("\n7\n", "\n257\n"),
        _VALID.replace("maf-exec-bytes-v1:stderr", "missing"),
        _VALID.replace("AP9h", "maf-exec-bytes-v1:stderr"),
        "unexpected prefix\n" + _VALID,
        _VALID + "unexpected suffix\n",
    ],
)
def test_malformed_or_inconsistent_envelopes_are_refused(damaged):
    with pytest.raises(ValueError):
        decode_envelope(damaged, "", 7)


def test_service_status_cannot_be_overridden_by_the_guest_frame():
    with pytest.raises(ValueError, match="exit status"):
        decode_envelope(_VALID, "", 137)


def test_encoder_diagnostics_are_not_guest_stderr():
    with pytest.raises(ValueError, match="diagnostics"):
        decode_envelope(_VALID, "base64: not found", 7)


def test_empty_streams_preserve_a_nonzero_exit_status():
    envelope = "maf-exec-bytes-v1:begin\n7\n\nmaf-exec-bytes-v1:stderr\n\nmaf-exec-bytes-v1:end\n"
    assert decode_envelope(envelope, "", 7) == Captured(b"", b"", 7)


@pytest.mark.parametrize("stdout", ["", "maf-exec-bytes-v1:begin", "maf-exec-bytes-v1:begin\n"])
def test_a_missing_status_line_is_recorded_as_a_failed_case(stdout):
    case = _case_result(ExecResult(stdout=stdout), b"", b"", 0)
    assert case["passed"] is False
    assert case["error"] == "ValueError"
    assert case["framed_exit_code"] is None


def test_failed_case_keeps_the_available_status_line():
    case = _case_result(ExecResult(stdout="maf-exec-bytes-v1:begin\n7\n", exit_code=7), b"", b"", 7)
    assert case["passed"] is False
    assert case["framed_exit_code"] == "7"


def test_complete_case_records_exact_streams():
    case = _case_result(ExecResult(stdout=_VALID, exit_code=7), b"\x00\xffa", b"e\xfe", 7)
    assert case["passed"] is True
    assert case["stdout"]["exact"] and case["stderr"]["exact"]


@pytest.mark.parametrize("exit_code,stderr", [(127, ""), (1, "find failed"), (0, "find: warning")])
def test_failed_inventory_cannot_report_zero_leftovers(exit_code, stderr):
    with pytest.raises(ValueError, match="inventory"):
        _count_capture_directories(ExecResult(stderr=stderr, exit_code=exit_code))


@pytest.mark.parametrize("stdout,count", [("", 0), ("/tmp/one\n/tmp/two\n", 2)])
def test_successful_inventory_counts_the_returned_directories(stdout, count):
    assert _count_capture_directories(ExecResult(stdout=stdout)) == count
