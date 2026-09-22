"""Validate bounded Windows directory records without a guest or Windows privileges."""

import pytest

from maf_sandbox_hyperlight._windows_files import _DIRECTORY_HEADER, _directory_records


def record(name="result.bin", *, following=0, length=None):
    encoded = name.encode("utf-16-le")
    identity = (2**100 + 123).to_bytes(16, "little")
    return (
        _DIRECTORY_HEADER.pack(
            following,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            len(encoded) if length is None else length,
            0,
            0,
            identity,
        )
        + encoded
    )


def test_directory_records_preserve_full_ids_and_names():
    first = record("first", following=104).ljust(104, b"\0")
    assert list(_directory_records(first + record("é.bin"))) == [
        ("first", 2**100 + 123),
        ("é.bin", 2**100 + 123),
    ]
    assert list(_directory_records(record("."))) == []
    assert list(_directory_records(record(".."))) == []


@pytest.mark.parametrize(
    "data",
    [
        b"",
        record()[:80],
        record(length=0),
        record(length=1),
        record(length=1000),
        record(following=8),
        record(following=89),
        record(following=4096),
        record("x")[:-2] + b"\x00\xd8",
    ],
)
def test_malformed_directory_record_refuses(data):
    with pytest.raises((OSError, UnicodeError)):
        list(_directory_records(data))
