"""Self-tests for native file probes; these modeled subjects are not native qualification."""

from __future__ import annotations

import asyncio

import pytest

from maf_sandbox import Capability
from maf_sandbox.native_conformance import (
    assert_files_delete_conformance,
    assert_files_in_conformance,
    assert_files_out_conformance,
    assert_reclaim_conformance,
)
from maf_sandbox.testing import InProcessSandbox


class Subject:
    working_directory = "/maf-sandbox/work"
    capabilities = frozenset(
        {
            Capability.FILES_IN,
            Capability.FILES_OUT,
            Capability.FILES_LIST,
            Capability.FILES_DELETE,
            Capability.RECLAIM,
        }
    )

    def __init__(self, *, corrupt=False, allows_links=False):
        self.sandbox = InProcessSandbox()
        self.sandbox.directories.add(self.working_directory)
        self.corrupt, self.allows_links = corrupt, allows_links

    async def read_in_guest(self, path):
        if self.corrupt:
            return b"wrong transport bytes"
        return self.sandbox.contents[self.working_directory + "/" + path]

    async def assert_links_refused(self):
        if self.allows_links:
            raise AssertionError("guest link creation succeeded")


@pytest.mark.parametrize(
    "suite",
    [
        assert_files_in_conformance,
        assert_files_out_conformance,
        assert_files_delete_conformance,
        assert_reclaim_conformance,
    ],
)
def test_native_probe_positive_control(suite):
    asyncio.run(suite(Subject()))


@pytest.mark.parametrize(
    "suite",
    [
        assert_files_in_conformance,
        assert_files_out_conformance,
        assert_files_delete_conformance,
        assert_reclaim_conformance,
    ],
)
@pytest.mark.parametrize("defect", ["corrupt", "allows_links"])
def test_native_probe_rejects_failed_guest_control(suite, defect):
    with pytest.raises(AssertionError):
        asyncio.run(suite(Subject(**{defect: True})))
