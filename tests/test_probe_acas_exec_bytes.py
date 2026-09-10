"""Keep the ACAS measurement from mistaking damaged framing for exact output."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from probe_acas_exec_bytes import Captured, decode_envelope  # noqa: E402

_VALID = (
    "maf-exec-bytes-v1:begin\n7\nAP9h\n\nmaf-exec-bytes-v1:stderr\nZf4=\n\nmaf-exec-bytes-v1:end\n"
)


def test_each_stream_is_recovered_without_text_decoding():
    assert decode_envelope(_VALID, "", 7) == Captured(b"\x00\xffa", b"e\xfe", 7)


def test_every_truncated_prefix_is_refused():
    for end in range(len(_VALID.rstrip("\n"))):
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
