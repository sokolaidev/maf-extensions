"""A native denial probe must not turn missing coverage or lost state into qualification."""

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
try:
    probe = importlib.import_module("scripts.experiments.mxc_session_patch.network_probe")
finally:
    sys.path.remove(str(Path(__file__).parents[1]))

CASES, validate = probe.CASES, probe.validate


def report():
    return {"sequence": 2, "denials": {name: 13 for name in CASES}}


def test_complete_matrix_requires_the_restored_sequence():
    value = report()
    assert len(validate(json.dumps(value).encode(), 2)) == 18
    with pytest.raises(ValueError, match="restoration"):
        validate(json.dumps(value).encode(), 1)


@pytest.mark.parametrize("error", [0, False, True, "13", None, 97, 38, 111])
def test_open_socket_or_unsupported_operation_is_not_a_denial(error):
    value = report()
    value["denials"]["libc-2-1"] = error
    with pytest.raises(ValueError, match="denial"):
        validate(json.dumps(value).encode(), 2)


@pytest.mark.parametrize("missing", list(CASES))
def test_each_native_and_python_case_is_required(missing):
    value = report()
    del value["denials"][missing]
    with pytest.raises(ValueError, match="denial"):
        validate(json.dumps(value).encode(), 2)


@pytest.mark.parametrize("value", [[], {}, {"sequence": 2}, {**report(), "extra": True}])
def test_incomplete_or_extended_record_is_refused(value):
    with pytest.raises(ValueError, match="record"):
        validate(json.dumps(value).encode(), 2)
