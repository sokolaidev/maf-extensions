"""Reject malformed host capture metadata before publishing a successful result."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "scripts/experiments/mxc_session_patch"
sys.path.insert(0, str(ROOT))
try:
    SPEC = importlib.util.spec_from_file_location("mxc_bounded_host", ROOT / "host_call.py")
    assert SPEC and SPEC.loader
    host = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(host)
finally:
    sys.path.remove(str(ROOT))


@pytest.fixture
def capture(tmp_path):
    report = tmp_path / "native.json"
    metadata = {
        "limit_bytes": 4,
        "retained_bytes": 4,
        "omitted_bytes": 8,
        "omitted_bytes_saturated": False,
        "truncated": True,
    }
    report.with_suffix(".output").write_bytes(b"abcd")
    return report, metadata


def write(report, metadata):
    report.write_text(json.dumps({"captured": True, "output": metadata}))


def test_native_metadata_is_separate_from_console_payload(capture):
    report, metadata = capture
    write(report, metadata)
    assert host.bounded_result(report, 4) == {"console_base64": "YWJjZA==", "output": metadata}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("limit_bytes", 5),
        ("retained_bytes", 5),
        ("retained_bytes", True),
        ("omitted_bytes", -1),
        ("omitted_bytes", 2**64),
        ("truncated", False),
        ("truncated", 1),
        ("omitted_bytes_saturated", True),
    ],
)
def test_inconsistent_metadata_is_refused(capture, key, value):
    report, metadata = capture
    metadata[key] = value
    write(report, metadata)
    with pytest.raises(host.Refused):
        host.bounded_result(report, 4)


def test_oversized_payload_is_refused_before_publication(capture):
    report, metadata = capture
    write(report, metadata)
    report.with_suffix(".output").write_bytes(b"abcde")
    with pytest.raises(host.Refused, match="length"):
        host.bounded_result(report, 4)


def test_guest_text_cannot_supply_native_completion(capture):
    report, _ = capture
    report.write_text('{"captured":true}')
    with pytest.raises(host.Refused, match="completion"):
        host.bounded_result(report, 4)


def test_control_size_is_checked_before_json_parse(capture):
    report, _ = capture
    report.write_bytes(b"x" * 1025)
    with pytest.raises(host.Refused, match="oversized"):
        host.bounded_result(report, 4)


PATCH_SPEC = importlib.util.spec_from_file_location("mxc_output_patch", ROOT / "output_patch.py")
assert PATCH_SPEC and PATCH_SPEC.loader
patch = importlib.util.module_from_spec(PATCH_SPEC)
PATCH_SPEC.loader.exec_module(patch)


def test_patch_state_refuses_partial_application_and_modified_files(tmp_path):
    files = {
        "existing.rs": {
            "before": hashlib.sha256(b"before\n").hexdigest(),
            "after": hashlib.sha256(b"after\n").hexdigest(),
        },
        "new.rs": {"before": None, "after": hashlib.sha256(b"new\n").hexdigest()},
    }
    (tmp_path / "existing.rs").write_bytes(b"before\r\n")
    assert patch.state(tmp_path, files) == "before"
    (tmp_path / "existing.rs").write_bytes(b"after\n")
    with pytest.raises(ValueError, match="modified or mixed"):
        patch.state(tmp_path, files)
    (tmp_path / "new.rs").write_bytes(b"new\n")
    assert patch.state(tmp_path, files) == "after"
    (tmp_path / "new.rs").write_bytes(b"edited\n")
    with pytest.raises(ValueError, match="modified or mixed"):
        patch.state(tmp_path, files)
