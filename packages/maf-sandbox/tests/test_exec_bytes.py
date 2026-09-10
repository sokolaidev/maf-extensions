"""Byte storage and display are different contracts, including timeout partial output."""

import dataclasses
import json

import pytest

from maf_sandbox import ExecResult, SandboxProgramTimeout

CORPUS = bytes(range(256)) + "\ufffd\r\n✓".encode() + b"\xe2\x82"


def test_bytes_remain_authoritative_and_text_is_serializable():
    result = ExecResult(stdout_bytes=CORPUS, stderr_bytes=CORPUS[::-1], exit_code=7)
    assert result.stdout_bytes == CORPUS
    assert result.stderr_bytes == CORPUS[::-1]
    assert result.stdout == result.stdout_text == CORPUS.decode("utf-8", "replace")
    assert result.stderr == result.stderr_text == CORPUS[::-1].decode("utf-8", "replace")
    json.dumps({"stdout": result.stdout, "stderr": result.stderr}, ensure_ascii=False).encode()
    assert dataclasses.replace(result, exit_code=9).stdout_bytes == CORPUS


def test_text_constructor_preserves_positional_ownership_and_utf8():
    result = ExecResult("✓", "host note", 7, True)
    assert result.stdout_bytes == "✓".encode()
    assert result.stderr_bytes == b"host note"
    assert result.producer_owns_stderr
    assert result.exit_code == 7


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_ambiguous_streams_are_rejected(stream):
    with pytest.raises(ValueError, match="not both"):
        ExecResult(**{stream: "text", stream + "_bytes": b"bytes"})


def test_timeout_preserves_bytes_beside_safe_display():
    timeout = SandboxProgramTimeout("timed out", output=CORPUS.decode("utf-8", "surrogateescape"))
    assert timeout.output_bytes == CORPUS
    assert timeout.output == CORPUS.decode("utf-8", "replace")
    json.dumps({"output": timeout.output}, ensure_ascii=False).encode()
